# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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

import torch
import torch.multiprocessing as mp
from datasets import load_dataset
from omegaconf import OmegaConf
from pytorch_lightning.trainer.trainer import Trainer
from tqdm import tqdm

import modelopt.torch.prune as mtp
from modelopt.torch.utils import print_rank_0

from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy
from nemo.core.config import hydra_runner
from nemo.utils.model_utils import load_config

mp.set_start_method("spawn", force=True)

"""
Nemo pruning example script.

Please consult examples/nlp/language_modeling/conf/megatron_gpt_prune.yaml config on available pruning arguments,
models supported as well as how to set up data and inference for calibration (with defaults recommended).

Example usage:
```
python examples/nlp/language_modeling/megatron_gpt_prune.py \
    model.restore_from_path=llama3-8b-base.nemo \
    model.tensor_model_parallel_size=1 \
    model.pipeline_model_parallel_size=8 \
    trainer.num_nodes=1 \
    trainer.precision=bf16 \
    trainer.devices=8 \
    export.decoder_type=llama \
    export.inference_tensor_parallel=1 \
    export.save_path=llama2-8b-base-pruned.nemo
```
where tensor_model_parallel_size must be 1 because of the current prune API limitation
"""


def get_calib_data_iter(data="cnn_dailymail", batch_size=64, calib_size=512, max_sequence_length=512):
    if data == "wikitext":
        dataset = load_dataset("wikitext", "wikitext-103-v1", split="train")
        text_column = "text"
    elif data == "cnn_dailymail":
        dataset = load_dataset("cnn_dailymail", name="3.0.0", split="train")
        text_column = "article"
    else:
        # Assume a local JSON dataset with a column named "text"
        dataset = load_dataset("json", data_files=data, split="train")
        text_column = "text"
    calib_size = max(min(len(dataset), calib_size), batch_size)
    for i in range(calib_size // batch_size):
        batch = dataset[i * batch_size : (i + 1) * batch_size][text_column]
        for j in range(len(batch)):
            batch[j] = batch[j][:max_sequence_length]
        yield batch

@hydra_runner(config_path="conf", config_name="megatron_gpt_prune")
def main(cfg) -> None:
    if not torch.cuda.is_available():
        raise EnvironmentError("GPU is required for the pruning.")

    # Overwrite model config with the one from the model checkpoint and apply pruning modifications
    model_cfg = load_config(cfg.model.restore_from_path)
    model_cfg.update(cfg.model)
    model_cfg.name = "modelopt"

    trainer = Trainer(strategy=NLPDDPStrategy(), **cfg.trainer)
    model = MegatronGPTModel.restore_from(
        restore_path=cfg.model.restore_from_path, override_config_path=model_cfg, trainer=trainer
    )
    model.freeze()

    data_iter = get_calib_data_iter(
        cfg.prune.calib_dataset,
        cfg.inference.batch_size,
        cfg.prune.num_calib_size,
        cfg.inference.max_context_length,
    )
    dataloader = [data for data in data_iter]

    def forward_loop(model):
        # NOTE: Alternatively you can also use `model.forward_bwd_step(data_iter, forward_only=True)`
        # if your model is setup for training.
        model.set_inference_config(OmegaConf.to_container(cfg.inference))
        for i, batch in enumerate(tqdm(dataloader, desc="Calibrating")):
            model.predict_step(batch, i)

    for name, param in model.state_dict().items():
        if param is not None:
            print_rank_0(f"model params before prune: {name} -> {param.type()} {param.shape}")

    model_pruned, _ = mtp.prune(
        model,
        mode="mcore_gpt_minitron",
        constraints={
            "export_config": {
                "ffn_hidden_size": cfg.prune.ffn_hidden_size,
                "num_attention_heads": cfg.prune.num_attention_heads,
                "num_query_groups": cfg.prune.num_query_groups,
            }
        },
        dummy_input=None,  # Not used
        config={
            "forward_loop": forward_loop,
        },
    )

    del model

    for name, param in model_pruned.state_dict().items():
        if param is not None:
            print_rank_0(f"model params after prune: {name} -> {param.type()} {param.shape}")

    # NOTE: Here we correct the pruned model configs here because the current API does not yet correct the configs internally
    # TODO: remove them once the API internally correct the configs

    kv_channels = model_cfg["kv_channels"] if "kv_channels" in model_cfg else None
    kv_channels = kv_channels if kv_channels is not None else model_cfg.hidden_size // model_cfg.num_attention_heads

    model_pruned.cfg["ffn_hidden_size"] = cfg.prune.ffn_hidden_size
    model_pruned.cfg["num_attention_heads"] = cfg.prune.num_attention_heads
    model_pruned.cfg["num_query_groups"] = cfg.prune.num_query_groups
    model_pruned.cfg["kv_channels"] = kv_channels

    model_pruned.save_to(cfg.export.save_path)

    torch.distributed.barrier()

    # TODO: read back and run inference

if __name__ == '__main__':
    main()
