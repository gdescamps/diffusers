# Copyright (c) 2023 Dominic Rampas MIT License
# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math

import numpy as np
import torch
import torch.nn as nn

from ...configuration_utils import ConfigMixin, register_to_config
from ...models.modeling_utils import ModelMixin
from .modeling_wuerstchen_common import AttnBlock, GlobalResponseNorm, TimestepBlock, WuerstchenLayerNorm


class ResBlockStageB(nn.Module):
    def __init__(self, c, c_skip=None, kernel_size=3, dropout=0.0):
        super().__init__()
        self.depthwise = nn.Conv2d(c, c, kernel_size=kernel_size, padding=kernel_size // 2, groups=c)
        self.norm = WuerstchenLayerNorm(c, elementwise_affine=False, eps=1e-6)
        self.channelwise = nn.Sequential(
            nn.Linear(c + c_skip, c * 4),
            nn.GELU(),
            GlobalResponseNorm(c * 4),
            nn.Dropout(dropout),
            nn.Linear(c * 4, c),
        )

    def forward(self, x, x_skip=None):
        x_res = x
        x = self.norm(self.depthwise(x))
        if x_skip is not None:
            x = torch.cat([x, x_skip], dim=1)
        x = self.channelwise(x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        return x + x_res


class WuerstchenDiffNeXt(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
        self,
        c_in=4,
        c_out=4,
        c_r=64,
        patch_size=2,
        c_cond=1024,
        c_hidden=[320, 640, 1280, 1280],
        nhead=[-1, 10, 20, 20],
        blocks=[4, 4, 14, 4],
        level_config=["CT", "CTA", "CTA", "CTA"],
        inject_effnet=[False, True, True, True],
        effnet_embd=16,
        clip_embd=1024,
        kernel_size=3,
        dropout=0.1,
    ):
        super().__init__()
        self.c_r = c_r
        self.c_cond = c_cond
        if not isinstance(dropout, list):
            dropout = [dropout] * len(c_hidden)

        # CONDITIONING
        self.clip_mapper = nn.Linear(clip_embd, c_cond)
        self.effnet_mappers = nn.ModuleList(
            [
                nn.Conv2d(effnet_embd, c_cond, kernel_size=1) if inject else None
                for inject in inject_effnet + list(reversed(inject_effnet))
            ]
        )
        self.seq_norm = nn.LayerNorm(c_cond, elementwise_affine=False, eps=1e-6)

        self.embedding = nn.Sequential(
            nn.PixelUnshuffle(patch_size),
            nn.Conv2d(c_in * (patch_size**2), c_hidden[0], kernel_size=1),
            WuerstchenLayerNorm(c_hidden[0], elementwise_affine=False, eps=1e-6),
        )

        def get_block(block_type, c_hidden, nhead, c_skip=0, dropout=0):
            if block_type == "C":
                return ResBlockStageB(c_hidden, c_skip, kernel_size=kernel_size, dropout=dropout)
            elif block_type == "A":
                return AttnBlock(c_hidden, c_cond, nhead, self_attn=True, dropout=dropout)
            elif block_type == "T":
                return TimestepBlock(c_hidden, c_r, conds=[])
            else:
                raise ValueError(f"Block type {block_type} not supported")

        # BLOCKS
        # -- down blocks
        self.down_blocks = nn.ModuleList()
        for i in range(len(c_hidden)):
            down_block = nn.ModuleList()
            if i > 0:
                down_block.append(
                    nn.Sequential(
                        WuerstchenLayerNorm(c_hidden[i - 1], elementwise_affine=False, eps=1e-6),
                        nn.Conv2d(c_hidden[i - 1], c_hidden[i], kernel_size=2, stride=2),
                    )
                )
            for _ in range(blocks[i]):
                for block_type in level_config[i]:
                    c_skip = c_cond if inject_effnet[i] else 0
                    down_block.append(get_block(block_type, c_hidden[i], nhead[i], c_skip=c_skip, dropout=dropout[i]))
            self.down_blocks.append(down_block)

        # -- up blocks
        self.up_blocks = nn.ModuleList()
        for i in reversed(range(len(c_hidden))):
            up_block = nn.ModuleList()
            for j in range(blocks[i]):
                for k, block_type in enumerate(level_config[i]):
                    c_skip = c_hidden[i] if i < len(c_hidden) - 1 and j == k == 0 else 0
                    c_skip += c_cond if inject_effnet[i] else 0
                    up_block.append(get_block(block_type, c_hidden[i], nhead[i], c_skip=c_skip, dropout=dropout[i]))
            if i > 0:
                up_block.append(
                    nn.Sequential(
                        WuerstchenLayerNorm(c_hidden[i], elementwise_affine=False, eps=1e-6),
                        nn.ConvTranspose2d(c_hidden[i], c_hidden[i - 1], kernel_size=2, stride=2),
                    )
                )
            self.up_blocks.append(up_block)

        # OUTPUT
        self.clf = nn.Sequential(
            WuerstchenLayerNorm(c_hidden[0], elementwise_affine=False, eps=1e-6),
            nn.Conv2d(c_hidden[0], 2 * c_out * (patch_size**2), kernel_size=1),
            nn.PixelShuffle(patch_size),
        )

        # --- WEIGHT INIT ---
        self.apply(self._init_weights)

    def _init_weights(self, m):
        # General init
        if isinstance(m, (nn.Conv2d, nn.Linear)):
            nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

        for mapper in self.effnet_mappers:
            if mapper is not None:
                nn.init.normal_(mapper.weight, std=0.02)  # conditionings
        nn.init.normal_(self.clip_mapper.weight, std=0.02)  # conditionings
        nn.init.xavier_uniform_(self.embedding[1].weight, 0.02)  # inputs
        nn.init.constant_(self.clf[1].weight, 0)  # outputs

        # blocks
        for level_block in self.down_blocks + self.up_blocks:
            for block in level_block:
                if isinstance(block, ResBlockStageB):
                    block.channelwise[-1].weight.data *= np.sqrt(1 / sum(self.config.blocks))
                elif isinstance(block, TimestepBlock):
                    nn.init.constant_(block.mapper.weight, 0)

    def gen_r_embedding(self, r, max_positions=10000):
        r = r * max_positions
        half_dim = self.c_r // 2
        emb = math.log(max_positions) / (half_dim - 1)
        emb = torch.arange(half_dim, device=r.device).float().mul(-emb).exp()
        emb = r[:, None] * emb[None, :]
        emb = torch.cat([emb.sin(), emb.cos()], dim=1)
        if self.c_r % 2 == 1:  # zero pad
            emb = nn.functional.pad(emb, (0, 1), mode="constant")
        return emb.to(dtype=r.dtype)

    def gen_c_embeddings(self, clip):
        clip = self.clip_mapper(clip)
        clip = self.seq_norm(clip)
        return clip

    def _down_encode(self, x, r_embed, effnet, clip=None):
        level_outputs = []
        for i, down_block in enumerate(self.down_blocks):
            effnet_c = None
            for block in down_block:
                if isinstance(block, ResBlockStageB):
                    if effnet_c is None and self.effnet_mappers[i] is not None:
                        dtype = effnet.dtype
                        effnet_c = self.effnet_mappers[i](
                            nn.functional.interpolate(
                                effnet.float(), size=x.shape[-2:], mode="bicubic", antialias=True, align_corners=True
                            ).to(dtype)
                        )
                    skip = effnet_c if self.effnet_mappers[i] is not None else None
                    x = block(x, skip)
                elif isinstance(block, AttnBlock):
                    x = block(x, clip)
                elif isinstance(block, TimestepBlock):
                    x = block(x, r_embed)
                else:
                    x = block(x)
            level_outputs.insert(0, x)
        return level_outputs

    def _up_decode(self, level_outputs, r_embed, effnet, clip=None):
        x = level_outputs[0]
        for i, up_block in enumerate(self.up_blocks):
            effnet_c = None
            for j, block in enumerate(up_block):
                if isinstance(block, ResBlockStageB):
                    if effnet_c is None and self.effnet_mappers[len(self.down_blocks) + i] is not None:
                        dtype = effnet.dtype
                        effnet_c = self.effnet_mappers[len(self.down_blocks) + i](
                            nn.functional.interpolate(
                                effnet.float(), size=x.shape[-2:], mode="bicubic", antialias=True, align_corners=True
                            ).to(dtype)
                        )
                    skip = level_outputs[i] if j == 0 and i > 0 else None
                    if effnet_c is not None:
                        if skip is not None:
                            skip = torch.cat([skip, effnet_c], dim=1)
                        else:
                            skip = effnet_c
                    x = block(x, skip)
                elif isinstance(block, AttnBlock):
                    x = block(x, clip)
                elif isinstance(block, TimestepBlock):
                    x = block(x, r_embed)
                else:
                    x = block(x)
        return x

    def forward(self, x, r, effnet, clip=None, x_cat=None, eps=1e-3, return_noise=True):
        if x_cat is not None:
            x = torch.cat([x, x_cat], dim=1)
        # Process the conditioning embeddings
        r_embed = self.gen_r_embedding(r)
        if clip is not None:
            clip = self.gen_c_embeddings(clip)

        # Model Blocks
        x_in = x
        x = self.embedding(x)
        level_outputs = self._down_encode(x, r_embed, effnet, clip)
        x = self._up_decode(level_outputs, r_embed, effnet, clip)
        a, b = self.clf(x).chunk(2, dim=1)
        b = b.sigmoid() * (1 - eps * 2) + eps
        if return_noise:
            return (x_in - a) / b
        else:
            return a, b


class WuerstchenDiffNeXt3(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
        self,
        c_in=4,
        c_out=4,
        c_r=64,
        patch_size=2,
        c_cond=1280,
        c_hidden=[320, 576, 1152, 1152],
        nhead=[-1, 9, 18, 18],
        blocks=[[2, 4, 14, 4], [4, 14, 4, 2]],
        block_repeat=[[1, 1, 1, 1], [2, 2, 2, 2]],
        level_config=["CT", "CT", "CTA", "CTA"],
        c_clip=1280,
        c_clip_seq=4,
        c_effnet=16,
        c_pixels=3,
        kernel_size=3,
        dropout=[0, 0, 0.1, 0.1],
        self_attn=True,
        t_conds=["sca"],
    ):
        super().__init__()
        self.c_r = c_r
        self.t_conds = t_conds
        self.c_clip_seq = c_clip_seq
        if not isinstance(dropout, list):
            dropout = [dropout] * len(c_hidden)
        if not isinstance(self_attn, list):
            self_attn = [self_attn] * len(c_hidden)

        # CONDITIONING
        self.effnet_mapper = nn.Sequential(
            nn.Conv2d(c_effnet, c_hidden[0] * 4, kernel_size=1),
            nn.GELU(),
            nn.Conv2d(c_hidden[0] * 4, c_hidden[0], kernel_size=1),
            WuerstchenLayerNorm(c_hidden[0], elementwise_affine=False, eps=1e-6),
        )
        self.pixels_mapper = nn.Sequential(
            nn.Conv2d(c_pixels, c_hidden[0] * 4, kernel_size=1),
            nn.GELU(),
            nn.Conv2d(c_hidden[0] * 4, c_hidden[0], kernel_size=1),
            WuerstchenLayerNorm(c_hidden[0], elementwise_affine=False, eps=1e-6),
        )
        self.clip_mapper = nn.Linear(c_clip, c_cond * c_clip_seq)
        self.clip_norm = nn.LayerNorm(c_cond, elementwise_affine=False, eps=1e-6)

        self.embedding = nn.Sequential(
            nn.PixelUnshuffle(patch_size),
            nn.Conv2d(c_in * (patch_size**2), c_hidden[0], kernel_size=1),
            WuerstchenLayerNorm(c_hidden[0], elementwise_affine=False, eps=1e-6),
        )

        def get_block(block_type, c_hidden, nhead, c_skip=0, dropout=0, self_attn=True):
            if block_type == "C":
                return ResBlockStageB(c_hidden, c_skip, kernel_size=kernel_size, dropout=dropout)
            elif block_type == "A":
                return AttnBlock(c_hidden, c_cond, nhead, self_attn=self_attn, dropout=dropout)
            elif block_type == "T":
                return TimestepBlock(c_hidden, c_r, conds=t_conds)
            else:
                raise Exception(f"Block type {block_type} not supported")

        # BLOCKS
        # -- down blocks
        self.down_blocks = nn.ModuleList()
        self.down_downscalers = nn.ModuleList()
        self.down_repeat_mappers = nn.ModuleList()
        for i in range(len(c_hidden)):
            if i > 0:
                self.down_downscalers.append(
                    nn.Sequential(
                        WuerstchenLayerNorm(c_hidden[i - 1], elementwise_affine=False, eps=1e-6),
                        nn.Conv2d(c_hidden[i - 1], c_hidden[i], kernel_size=2, stride=2),
                    )
                )
            else:
                self.down_downscalers.append(nn.Identity())
            down_block = nn.ModuleList()
            for _ in range(blocks[0][i]):
                for block_type in level_config[i]:
                    block = get_block(block_type, c_hidden[i], nhead[i], dropout=dropout[i], self_attn=self_attn[i])
                    down_block.append(block)
            self.down_blocks.append(down_block)
            if block_repeat is not None:
                block_repeat_mappers = nn.ModuleList()
                for _ in range(block_repeat[0][i] - 1):
                    block_repeat_mappers.append(nn.Conv2d(c_hidden[i], c_hidden[i], kernel_size=1))
                self.down_repeat_mappers.append(block_repeat_mappers)

        # -- up blocks
        self.up_blocks = nn.ModuleList()
        self.up_upscalers = nn.ModuleList()
        self.up_repeat_mappers = nn.ModuleList()
        for i in reversed(range(len(c_hidden))):
            if i > 0:
                self.up_upscalers.append(
                    nn.Sequential(
                        WuerstchenLayerNorm(c_hidden[i], elementwise_affine=False, eps=1e-6),
                        nn.ConvTranspose2d(c_hidden[i], c_hidden[i - 1], kernel_size=2, stride=2),
                    )
                )
            else:
                self.up_upscalers.append(nn.Identity())
            up_block = nn.ModuleList()
            for j in range(blocks[1][::-1][i]):
                for k, block_type in enumerate(level_config[i]):
                    c_skip = c_hidden[i] if i < len(c_hidden) - 1 and j == k == 0 else 0
                    block = get_block(
                        block_type, c_hidden[i], nhead[i], c_skip=c_skip, dropout=dropout[i], self_attn=self_attn[i]
                    )
                    up_block.append(block)
            self.up_blocks.append(up_block)
            if block_repeat is not None:
                block_repeat_mappers = nn.ModuleList()
                for _ in range(block_repeat[1][::-1][i] - 1):
                    block_repeat_mappers.append(nn.Conv2d(c_hidden[i], c_hidden[i], kernel_size=1))
                self.up_repeat_mappers.append(block_repeat_mappers)

        # OUTPUT
        self.clf = nn.Sequential(
            WuerstchenLayerNorm(c_hidden[0], elementwise_affine=False, eps=1e-6),
            nn.Conv2d(c_hidden[0], c_out * (patch_size**2), kernel_size=1),
            nn.PixelShuffle(patch_size),
        )

        # --- WEIGHT INIT ---
        self.apply(self._init_weights)

    def _init_weights(self, m):
        # General init
        if isinstance(m, (nn.Conv2d, nn.Linear)):
            torch.nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

        nn.init.normal_(self.clip_mapper.weight, std=0.02)  # conditionings
        nn.init.normal_(self.effnet_mapper[0].weight, std=0.02)  # conditionings
        nn.init.normal_(self.effnet_mapper[2].weight, std=0.02)  # conditionings
        nn.init.normal_(self.pixels_mapper[0].weight, std=0.02)  # conditionings
        nn.init.normal_(self.pixels_mapper[2].weight, std=0.02)  # conditionings
        torch.nn.init.xavier_uniform_(self.embedding[1].weight, 0.02)  # inputs
        nn.init.constant_(self.clf[1].weight, 0)  # outputs

        # blocks
        for level_block in self.down_blocks + self.up_blocks:
            for block in level_block:
                if isinstance(block, ResBlockStageB):
                    block.channelwise[-1].weight.data *= np.sqrt(1 / sum(self.config.blocks[0]))
                elif isinstance(block, TimestepBlock):
                    nn.init.constant_(block.mapper.weight, 0)

    def gen_r_embedding(self, r, max_positions=10000):
        r = r * max_positions
        half_dim = self.c_r // 2
        emb = math.log(max_positions) / (half_dim - 1)
        emb = torch.arange(half_dim, device=r.device).float().mul(-emb).exp()
        emb = r[:, None] * emb[None, :]
        emb = torch.cat([emb.sin(), emb.cos()], dim=1)
        if self.c_r % 2 == 1:  # zero pad
            emb = nn.functional.pad(emb, (0, 1), mode="constant")
        return emb

    def gen_c_embeddings(self, clip):
        if len(clip.shape) == 2:
            clip = clip.unsqueeze(1)
        clip = self.clip_mapper(clip).view(clip.size(0), clip.size(1) * self.c_clip_seq, -1)
        clip = self.clip_norm(clip)
        return clip

    def _down_encode(self, x, r_embed, clip):
        level_outputs = []
        block_group = zip(self.down_blocks, self.down_downscalers, self.down_repeat_mappers)
        for down_block, downscaler, repmap in block_group:
            x = downscaler(x)
            for i in range(len(repmap) + 1):
                for block in down_block:
                    if isinstance(block, ResBlockStageB):
                        x = block(x)
                    elif isinstance(block, AttnBlock):
                        x = block(x, clip)
                    elif isinstance(block, TimestepBlock):
                        x = block(x, r_embed)
                    else:
                        x = block(x)
                if i < len(repmap):
                    x = repmap[i](x)
            level_outputs.insert(0, x)
        return level_outputs

    def _up_decode(self, level_outputs, r_embed, clip):
        x = level_outputs[0]
        block_group = zip(self.up_blocks, self.up_upscalers, self.up_repeat_mappers)
        for i, (up_block, upscaler, repmap) in enumerate(block_group):
            for j in range(len(repmap) + 1):
                for k, block in enumerate(up_block):
                    if isinstance(block, ResBlockStageB):
                        skip = level_outputs[i] if k == 0 and i > 0 else None
                        if skip is not None and (x.size(-1) != skip.size(-1) or x.size(-2) != skip.size(-2)):
                            x = torch.nn.functional.interpolate(
                                x.float(), skip.shape[-2:], mode="bilinear", align_corners=True
                            )
                        x = block(x, skip)
                    elif isinstance(block, AttnBlock):
                        x = block(x, clip)
                    elif isinstance(block, TimestepBlock):
                        x = block(x, r_embed)
                    else:
                        x = block(x)
                if j < len(repmap):
                    x = repmap[j](x)
            x = upscaler(x)
        return x

    def forward(self, x, r, effnet, clip, pixels=None, **kwargs):
        if pixels is None:
            pixels = x.new_zeros(x.size(0), 3, 8, 8)

        # Process the conditioning embeddings
        r_embed = self.gen_r_embedding(r)
        for c in self.t_conds:
            t_cond = kwargs.get(c, torch.ones_like(r))
            r_embed = torch.cat([r_embed, self.gen_r_embedding(t_cond)], dim=1)
        clip = self.gen_c_embeddings(clip)

        # Model Blocks
        x = self.embedding(x)
        x = x + self.effnet_mapper(
            nn.functional.interpolate(effnet.float(), size=x.shape[-2:], mode="bilinear", align_corners=True)
        )
        x = x + nn.functional.interpolate(
            self.pixels_mapper(pixels).float(), size=x.shape[-2:], mode="bilinear", align_corners=True
        )
        level_outputs = self._down_encode(x, r_embed, clip)
        x = self._up_decode(level_outputs, r_embed, clip)
        return self.clf(x)
