"""Implementation of the paper:

LLaMA-Adapter: Efficient Fine-tuning of Language Models with Zero-init Attention
https://arxiv.org/abs/2303.16199

Port for Lit-Parrot
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple, Any, List, Union

import torch
import torch.nn as nn
from torch.nn import functional as F
from typing_extensions import Self

from lit_parrot.config import Config as BaseConfig
from lit_parrot.model import (
    Parrot as BaseModel,
    MLP,
    CausalSelfAttention as BaseCausalSelfAttention,
    apply_rope,
    RoPECache,
    KVCache,
)


@dataclass
class Config(BaseConfig):
    adapter_prompt_length: int = 10
    adapter_start_layer: int = 2


class Parrot(BaseModel):
    """The implementation is identical to `lit_parrot.model.Parrot` with the exception that
    the `Block` saves the layer index and passes it down to the attention layer."""

    def __init__(self, config: Config) -> None:
        nn.Module.__init__(self)
        assert config.padded_vocab_size is not None
        self.config = config

        self.lm_head = nn.Linear(config.n_embd, config.padded_vocab_size, bias=False)
        self.transformer = nn.ModuleDict(
            dict(
                wte=nn.Embedding(config.padded_vocab_size, config.n_embd),
                h=nn.ModuleList(Block(config, i) for i in range(config.n_layer)),
                ln_f=nn.LayerNorm(config.n_embd),
            )
        )

        self.rope_cache: Optional[RoPECache] = None
        self.mask_cache: Optional[torch.Tensor] = None
        self.kv_caches: List[KVCache] = []
        self.adapter_kv_caches: List[KVCache] = []

    def reset_cache(self) -> None:
        super().reset_cache()
        self.adapter_kv_caches.clear()

    def forward(
        self, idx: torch.Tensor, max_seq_length: Optional[int] = None, input_pos: Optional[torch.Tensor] = None
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, List[KVCache], List[KVCache]]]:
        B, T = idx.size()
        use_kv_cache = input_pos is not None

        block_size = self.config.block_size
        if max_seq_length is None:
            max_seq_length = block_size
        assert T <= max_seq_length, f"Cannot forward sequence of length {T}, max seq length is only {max_seq_length}"
        assert max_seq_length <= block_size, f"Cannot attend to {max_seq_length}, block size is only {block_size}"
        assert T <= block_size, f"Cannot forward sequence of length {T}, block size is only {block_size}"

        if self.rope_cache is None:
            self.rope_cache = self.build_rope_cache(idx)
        # passing `attn_mask` to SDPA downgrades it to use the inefficient implementation. since we only need the mask
        # for the kv-cache support (only during inference), we only create it in that situation
        # this will be resolved by https://github.com/pytorch/pytorch/issues/96099
        if use_kv_cache and self.mask_cache is None:
            self.mask_cache = self.build_mask_cache(idx)

        cos, sin = self.rope_cache
        if use_kv_cache:
            cos = cos.index_select(0, input_pos)
            sin = sin.index_select(0, input_pos)
            mask = self.mask_cache.index_select(2, input_pos)
            mask = mask[:, :, :, :max_seq_length]
        else:
            cos = cos[:T]
            sin = sin[:T]
            mask = None

        # forward the model itself
        x = self.transformer.wte(idx)  # token embeddings of shape (b, t, n_embd)

        if not use_kv_cache:
            for block in self.transformer.h:
                x, *_ = block(x, (cos, sin), max_seq_length)
        else:
            self.kv_caches = self.kv_caches or self.build_kv_caches(x, max_seq_length, cos.size(-1))
            self.adapter_kv_caches = self.adapter_kv_caches or [None for _ in range(self.config.n_layer)]
            for i, block in enumerate(self.transformer.h):
                x, self.kv_caches[i], self.adapter_kv_caches[i] = block(
                    x, (cos, sin), max_seq_length, mask, input_pos, self.kv_caches[i], self.adapter_kv_caches[i]
                )

        x = self.transformer.ln_f(x)

        logits = self.lm_head(x)  # (b, t, vocab_size)

        return logits

    @classmethod
    def from_name(cls, name: str, **kwargs: Any) -> Self:
        return cls(Config.from_name(name, **kwargs))


class Block(nn.Module):
    """The implementation is identical to `lit_parrot.model.Block` with the exception that
    we replace the attention layer where adaption is implemented."""

    def __init__(self, config: Config, block_idx: int) -> None:
        super().__init__()
        self.norm_1 = nn.LayerNorm(config.n_embd)
        self.attn = CausalSelfAttention(config, block_idx)
        if not config.shared_attention_norm:
            self.norm_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)

        self.config = config

    def forward(
        self,
        x: torch.Tensor,
        rope: RoPECache,
        max_seq_length: int,
        mask: Optional[torch.Tensor] = None,
        input_pos: Optional[torch.Tensor] = None,
        kv_cache: Optional[KVCache] = None,
        adapter_kv_cache: Optional[KVCache] = None,
    ) -> Tuple[torch.Tensor, Optional[KVCache], Optional[KVCache]]:
        n_1 = self.norm_1(x)
        h, new_kv_cache, new_adapter_kv_cache = self.attn(
            n_1, rope, max_seq_length, mask, input_pos, kv_cache, adapter_kv_cache
        )
        if self.config.parallel_residual:
            n_2 = n_1 if self.config.shared_attention_norm else self.norm_2(x)
            x = x + h + self.mlp(n_2)
        else:
            if self.config.shared_attention_norm:
                raise NotImplementedError(
                    "No checkpoint amongst the ones we support uses this configuration"
                    " (non-parallel residual and shared attention norm)."
                )
            x = x + h
            x = x + self.mlp(self.norm_2(x))
        return x, new_kv_cache, new_adapter_kv_cache


class CausalSelfAttention(BaseCausalSelfAttention):
    """A modification of `lit_parrot.model.CausalSelfAttention` that adds the attention
    over the adaption prompt."""

    def __init__(self, config: Config, block_idx: int) -> None:
        super().__init__(config)
        if block_idx >= config.adapter_start_layer:
            # adapter embedding layer
            self.adapter_wte = nn.Embedding(config.adapter_prompt_length, config.n_embd)
            # gate for adaption
            self.gating_factor = torch.nn.Parameter(torch.zeros(1, config.n_head, 1, 1))
        self.block_idx = block_idx

    def forward(
        self,
        x: torch.Tensor,
        rope: RoPECache,
        max_seq_length: int,
        mask: Optional[torch.Tensor] = None,
        input_pos: Optional[torch.Tensor] = None,
        kv_cache: Optional[KVCache] = None,
        adapter_kv_cache: Optional[KVCache] = None,
    ) -> Tuple[torch.Tensor, Optional[KVCache], Optional[KVCache]]:
        B, T, C = x.size()  # batch size, sequence length, embedding dimensionality (n_embd)

        qkv = self.attn(x)

        # assemble into a number of query groups to support MHA, MQA and GQA together (see `config.n_query_groups`)
        q_per_kv = self.config.n_head // self.config.n_query_groups
        # each group has 1+ queries, 1 key, and 1 value (hence the + 2)
        qkv = qkv.view(B, T, self.config.n_query_groups, q_per_kv + 2, self.config.head_size).permute(0, 2, 3, 1, 4)
        # split batched computation into three
        q, k, v = qkv.split((q_per_kv, 1, 1), dim=2)
        if self.config.n_query_groups != 1:  # doing this would require a full kv cache with MQA (inefficient!)
            # for MHA this is a no-op
            k = k.repeat_interleave(q_per_kv, dim=2)
            v = v.repeat_interleave(q_per_kv, dim=2)
        q = q.reshape(B, -1, T, self.config.head_size)  # (B, nh_q, T, hs)
        k = k.view(B, -1, T, self.config.head_size)  # (B, nh_k, T, hs)
        v = v.view(B, -1, T, self.config.head_size)  # (B, nh_v, T, hs)

        n_elem = int(self.config.rotary_percentage * self.config.head_size)

        cos, sin = rope
        q_roped = apply_rope(q[..., :n_elem], cos, sin)
        k_roped = apply_rope(k[..., :n_elem], cos, sin)
        q = torch.cat((q_roped, q[..., n_elem:]), dim=-1)
        k = torch.cat((k_roped, k[..., n_elem:]), dim=-1)

        if kv_cache is not None:
            cache_k, cache_v = kv_cache
            cache_k, cache_v = cache_k.to(dtype=k.dtype), cache_v.to(dtype=v.dtype)
            # check if reached token limit
            if input_pos[-1] >= max_seq_length:
                input_pos = torch.tensor(max_seq_length - 1, device=input_pos.device)
                # shift 1 position to the left
                cache_k = torch.roll(cache_k, -1, dims=2)
                cache_v = torch.roll(cache_v, -1, dims=2)
            k = cache_k.index_copy(2, input_pos, k)
            v = cache_v.index_copy(2, input_pos, v)
            kv_cache = k, v

        # efficient attention using Flash Attention CUDA kernels
        y = F.scaled_dot_product_attention(
            q, k, v, attn_mask=mask, dropout_p=0.0, scale=1.0 / math.sqrt(self.config.head_size), is_causal=mask is None
        )

        if self.block_idx >= self.config.adapter_start_layer:
            aT = self.config.adapter_prompt_length
            if adapter_kv_cache is not None:
                ak, av = adapter_kv_cache
            else:
                prefix = self.adapter_wte.weight.reshape(1, aT, C)
                aqkv = self.attn(prefix)
                aqkv = aqkv.view(1, aT, self.config.n_query_groups, q_per_kv + 2, self.config.head_size)
                aqkv = aqkv.permute(0, 2, 3, 1, 4)
                _, ak, av = aqkv.split((q_per_kv, 1, 1), dim=2)
                ak = ak.repeat_interleave(B, dim=0)
                av = av.repeat_interleave(B, dim=0)
                ak = ak.view(B, -1, aT, self.config.head_size)  # (B, nh_ak, aT, hs)
                av = av.view(B, -1, aT, self.config.head_size)  # (B, nh_av, aT, hs)
                adapter_kv_cache = (ak, av)

            amask = torch.ones(T, aT, dtype=torch.bool, device=x.device)
            ay = F.scaled_dot_product_attention(q, ak, av, attn_mask=amask, dropout_p=0.0, is_causal=False)
            y = y + self.gating_factor * ay

        y = y.transpose(1, 2).contiguous().view(B, T, C)  # re-assemble all head outputs side by side

        # output projection
        y = self.proj(y)

        return y, kv_cache, adapter_kv_cache


def mark_only_adapter_as_trainable(model: Parrot) -> None:
    """Sets `requires_grad=False` for all non-adapter weights."""
    for name, param in model.named_parameters():
        param.requires_grad = "adapter_wte" in name or "gating_factor" in name


def adapter_state_from_state_dict(state_dict: dict) -> dict:
    """Returns the model state dict with only the adapter weights for saving."""
    return {name: param for name, param in state_dict.items() if "adapter_wte" in name or "gating_factor" in name}
