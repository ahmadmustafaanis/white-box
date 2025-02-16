from datasets import Dataset, DatasetDict
from functools import partial
from multiprocessing import cpu_count
from transformers import PreTrainedTokenizerBase
from typing import TypeVar, Union
import logging
import math


T = TypeVar("T", bound=Union[Dataset, DatasetDict])


def chunk_and_tokenize(
    data: T,
    tokenizer: PreTrainedTokenizerBase,
    *,
    format: str = "torch",
    text_key: str = "text",
) -> T:
    """Perform GPT-style chunking and tokenization on a dataset.

    The resulting dataset will consist entirely of chunks exactly `max_length` tokens
    long. Long sequences will be split into multiple chunks, and short sequences will
    be merged with their neighbors, using `eos_token` as a separator.

    Args:
        data: The dataset to chunk and tokenize.
        tokenizer: The tokenizer to use.
        format: The format to return the dataset in, passed to `Dataset.with_format`.
        text_key: The key in the dataset to use as the text to tokenize.

    Returns:
        The chunked and tokenized dataset.
    """
    return data.map(
        partial(_tokenize_fn, tokenizer=tokenizer, text_key=text_key),
        batched=True,
        num_proc=cpu_count() // 2,
        remove_columns=get_columns_all_equal(data),
    ).with_format(
        format,
        # Remove the "overflow_to_sample_mapping" column so we can directly pass
        # elements of the dataset to a model
        columns=["input_ids", "attention_mask"],
    )


def compute_nats_to_bpb_ratio(raw: T, tokenized: T) -> float:
    """Compute ratio of nats per token to bits per byte for a given tokenizer.

    This is used to convert the perplexity of a model to bits per byte.

    Args:
        raw: The raw, unprocessed dataset.
        tokenized: The tokenized dataset.

    Returns:
        The ratio of nats to bits per byte.
    """
    byte_counts = raw.map(
        lambda x: {"length": [len(txt.encode("utf-8")) for txt in x["text"]]},
        batched=True,
        num_proc=cpu_count() // 2,
        remove_columns=get_columns_all_equal(raw),
    )

    token_counts = tokenized.map(
        lambda x: {"length": [len(ids) for ids in x["input_ids"]]},
        batched=True,
        num_proc=cpu_count() // 2,
        remove_columns=get_columns_all_equal(tokenized),
    )
    total_bytes = sum(byte_counts["length"])  # type: ignore[operator]
    total_tokens = sum(token_counts["length"])  # type: ignore[operator]

    # See https://arxiv.org/pdf/2101.00027.pdf, section 3.1
    return (total_tokens / total_bytes) / math.log(2)


def _tokenize_fn(x: dict, tokenizer: PreTrainedTokenizerBase, text_key: str):
    """Annoyingly, we need to use a separate function so it can be hashed correctly."""
    sep = tokenizer.eos_token or "<|endoftext|>"
    return {
        # We know that the last sample will almost always be less than the max
        # number of tokens, and we don't want to pad, so we just drop it.
        k: v[:-1]
        for k, v in tokenizer(
            # Concatenate all the samples together, separated by the EOS token.
            sep.join(x[text_key]),
            max_length=min(tokenizer.model_max_length, 2048),
            return_overflowing_tokens=True,
            truncation=True,
        ).items()
    }


def get_columns_all_equal(dataset: Union[Dataset, DatasetDict]) -> list[str]:
    """Get a single list of columns in a `Dataset` or `DatasetDict`.

    We assert the columms are the same across splits if it's a `DatasetDict`.

    Args:
        dataset: The dataset to get the columns from.

    Returns:
        A list of columns.
    """
    if isinstance(dataset, DatasetDict):
        cols_by_split = dataset.column_names.values()
        columns = next(iter(cols_by_split))
        if not all(cols == columns for cols in cols_by_split):
            raise ValueError("All splits must have the same columns")

        return columns

    return dataset.column_names


def silence_datasets_messages():
    """Silence the very annoying wall of 'Loading cached processed dataset' messages."""
    handler = logging.StreamHandler()
    handler.addFilter(lambda log_record: "cached" not in log_record.getMessage())
    logging.getLogger("datasets").addHandler(handler)
