import torch


def test_lora_layer_replacement():
    from lit_parrot.lora import lora, CausalSelfAttention as LoRACausalSelfAttention
    from lit_parrot.model import Parrot, Config

    config = Config(n_layer=2, n_head=4, n_embd=8, block_size=8, vocab_size=8)
    with lora(r=8, alpha=8, dropout=0.1):
        model = Parrot(config)

    assert isinstance(model.transformer.h[0].attn, LoRACausalSelfAttention)
    assert isinstance(model.transformer.h[1].attn, LoRACausalSelfAttention)


def test_lora_merge_unmerge():
    from lit_parrot.lora import lora, mark_only_lora_as_trainable
    from lit_parrot.model import Parrot, Config

    config = Config(n_layer=1, n_head=2, n_embd=8, block_size=8, vocab_size=8)
    with lora(r=8, alpha=8, dropout=0.1):
        model = Parrot(config)

    initial_weight = model.transformer.h[0].attn.attn.weight.clone()
    model.train()
    assert torch.equal(model.transformer.h[0].attn.attn.weight, initial_weight)

    # perform an update to the LoRA weights
    mark_only_lora_as_trainable(model)
    optimizer = torch.optim.SGD(model.parameters(), lr=1.0)
    model(torch.randint(0, 8, size=(2, 4), dtype=torch.int64)).sum().backward()
    optimizer.step()
    optimizer.zero_grad()
    # the weight remains unchanged (only lora A and B change)
    assert torch.equal(model.transformer.h[0].attn.attn.weight, initial_weight)

    # 'merge' and then 'unmerge' should neutralize themselves
    weight_before = model.transformer.h[0].attn.attn.weight.clone()
    model.eval()
    assert not torch.equal(model.transformer.h[0].attn.attn.weight, weight_before)
    model.train()
    # note: numerically, `W + (A * B) - (A * B) == W` does not hold exactly
    torch.testing.assert_close(model.transformer.h[0].attn.attn.weight, weight_before)

    # calling eval/train multiple times in a row should not merge/unmerge multiple times
    model.eval()
    assert model.transformer.h[0].attn.attn.merged
    weight_after = model.transformer.h[0].attn.attn.weight.clone()
    model.eval()
    model.eval()
    assert torch.equal(model.transformer.h[0].attn.attn.weight, weight_after)
    model.train()
    assert not model.transformer.h[0].attn.attn.merged
    weight_after = model.transformer.h[0].attn.attn.weight.clone()
    model.train()
    model.train()
    assert torch.equal(model.transformer.h[0].attn.attn.weight, weight_after)
