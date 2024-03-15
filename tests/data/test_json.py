# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.
import json
import pytest


def test_json(tmp_path, mock_tokenizer):
    from litgpt.data import JSON
    from litgpt.prompts import PromptStyle

    class Style(PromptStyle):
        def apply(self, prompt, **kwargs):
            return f"X: {prompt} {kwargs['input']} Y:"

    json_path = tmp_path / "data.json"
    mock_data = [
        {"instruction": "Add", "input": "2+2", "output": "4"},
        {"instruction": "Subtract", "input": "5-3", "output": "2"},
        {"instruction": "Multiply", "input": "6*4", "output": "24"},
        {"instruction": "Divide", "input": "10/2", "output": "5"},
        {"instruction": "Exponentiate", "input": "2^3", "output": "8"},
        {"instruction": "Square root", "input": "√9", "output": "3"},
    ]

    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump(mock_data, fp)

    with pytest.raises(FileNotFoundError):
        JSON(tmp_path / "not exist")

    # TODO: Make prompt template an argumenet
    data = JSON(json_path, test_split_fraction=0.5, prompt_style=Style(), num_workers=0)
    data.connect(tokenizer=mock_tokenizer, batch_size=2)
    data.prepare_data()  # does nothing
    data.setup()

    train_dataloader = data.train_dataloader()
    val_dataloader = data.val_dataloader()

    assert len(train_dataloader) == 2
    assert len(val_dataloader) == 2

    train_data = list(train_dataloader)
    val_data = list(val_dataloader)

    assert train_data[0]["input_ids"].size(0) == 2
    assert train_data[1]["input_ids"].size(0) == 1
    assert val_data[0]["input_ids"].size(0) == 2
    assert val_data[1]["input_ids"].size(0) == 1

    assert mock_tokenizer.decode(train_data[0]["input_ids"][0]).startswith("X: Divide 10/2 Y:5")
    assert mock_tokenizer.decode(train_data[0]["input_ids"][1]).startswith("X: Add 2+2 Y:4")
    assert mock_tokenizer.decode(train_data[1]["input_ids"][0]).startswith("X: Multiply 6*4 Y:24")

    assert mock_tokenizer.decode(val_data[0]["input_ids"][0]).startswith("X: Exponentiate 2^3 Y:8")
    assert mock_tokenizer.decode(val_data[0]["input_ids"][1]).startswith("X: Subtract 5-3 Y:2")
    assert mock_tokenizer.decode(val_data[1]["input_ids"][0]).startswith("X: Square root √9 Y:3")

    assert isinstance(train_dataloader.dataset.prompt_style, Style)
    assert isinstance(val_dataloader.dataset.prompt_style, Style)

