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
import os
import re
from contextlib import nullcontext
from typing import Callable, Dict, List, Optional, Union

import safetensors
import torch
from huggingface_hub import model_info
from packaging import version
from torch import nn

from .. import __version__
from ..models.modeling_utils import _LOW_CPU_MEM_USAGE_DEFAULT, load_model_dict_into_meta
from ..utils import (
    DIFFUSERS_CACHE,
    HF_HUB_OFFLINE,
    USE_PEFT_BACKEND,
    _get_model_file,
    convert_state_dict_to_diffusers,
    convert_state_dict_to_peft,
    convert_unet_state_dict_to_peft,
    delete_adapter_layers,
    deprecate,
    get_adapter_name,
    get_peft_kwargs,
    is_accelerate_available,
    is_transformers_available,
    logging,
    recurse_remove_peft_layers,
    scale_lora_layers,
    set_adapter_layers,
    set_weights_and_activate_adapters,
)


if is_transformers_available():
    from transformers import CLIPTextModel, CLIPTextModelWithProjection

    # To be deprecated soon
    from ..models.lora import PatchedLoraProjection

if is_accelerate_available():
    from accelerate import init_empty_weights
    from accelerate.hooks import AlignDevicesHook, CpuOffload, remove_hook_from_module

logger = logging.get_logger(__name__)

TEXT_ENCODER_NAME = "text_encoder"
UNET_NAME = "unet"

LORA_WEIGHT_NAME = "pytorch_lora_weights.bin"
LORA_WEIGHT_NAME_SAFE = "pytorch_lora_weights.safetensors"

LORA_DEPRECATION_MESSAGE = "You are using an old version of LoRA backend. This will be deprecated in the next releases in favor of PEFT make sure to install the latest PEFT and transformers packages in the future."


def text_encoder_attn_modules(text_encoder):
    attn_modules = []

    if isinstance(text_encoder, (CLIPTextModel, CLIPTextModelWithProjection)):
        for i, layer in enumerate(text_encoder.text_model.encoder.layers):
            name = f"text_model.encoder.layers.{i}.self_attn"
            mod = layer.self_attn
            attn_modules.append((name, mod))
    else:
        raise ValueError(f"do not know how to get attention modules for: {text_encoder.__class__.__name__}")

    return attn_modules


def text_encoder_mlp_modules(text_encoder):
    mlp_modules = []

    if isinstance(text_encoder, (CLIPTextModel, CLIPTextModelWithProjection)):
        for i, layer in enumerate(text_encoder.text_model.encoder.layers):
            mlp_mod = layer.mlp
            name = f"text_model.encoder.layers.{i}.mlp"
            mlp_modules.append((name, mlp_mod))
    else:
        raise ValueError(f"do not know how to get mlp modules for: {text_encoder.__class__.__name__}")

    return mlp_modules


class LoraLoaderMixin:
    r"""
    Load LoRA layers into [`UNet2DConditionModel`] and [`~transformers.CLIPTextModel`].
    """

    text_encoder_name = TEXT_ENCODER_NAME
    unet_name = UNET_NAME
    num_fused_loras = 0

    def load_lora_weights(
        self, pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]], adapter_name=None, **kwargs
    ):
        """
        Load LoRA weights specified in `pretrained_model_name_or_path_or_dict` into `self.unet` and
        `self.text_encoder`.

        All kwargs are forwarded to `self.lora_state_dict`.

        See [`~loaders.LoraLoaderMixin.lora_state_dict`] for more details on how the state dict is loaded.

        See [`~loaders.LoraLoaderMixin.load_lora_into_unet`] for more details on how the state dict is loaded into
        `self.unet`.

        See [`~loaders.LoraLoaderMixin.load_lora_into_text_encoder`] for more details on how the state dict is loaded
        into `self.text_encoder`.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                A string (model id of a pretrained model hosted on the Hub), a path to a directory containing the model
                weights, or a [torch state
                dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).
            kwargs (`dict`, *optional*):
                See [`~loaders.LoraLoaderMixin.lora_state_dict`].
            adapter_name (`str`, *optional*):
                Name for referencing the loaded adapter model. If not specified, it will use `default_{i}` where `i` is
                the total number of adapters being loaded. Must have PEFT installed to use.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16).to(
            "cuda"
        )
        pipeline.load_lora_weights(
            "Yntec/pineappleAnimeMix", weight_name="pineappleAnimeMix_pineapple10.1.safetensors", adapter_name="anime"
        )
        ```
        """
        # First, ensure that the checkpoint is a compatible one and can be successfully loaded.
        state_dict, network_alphas = self.lora_state_dict(pretrained_model_name_or_path_or_dict, **kwargs)

        is_correct_format = all("lora" in key for key in state_dict.keys())
        if not is_correct_format:
            raise ValueError("Invalid LoRA checkpoint.")

        low_cpu_mem_usage = kwargs.pop("low_cpu_mem_usage", _LOW_CPU_MEM_USAGE_DEFAULT)

        self.load_lora_into_unet(
            state_dict,
            network_alphas=network_alphas,
            unet=getattr(self, self.unet_name) if not hasattr(self, "unet") else self.unet,
            low_cpu_mem_usage=low_cpu_mem_usage,
            adapter_name=adapter_name,
            _pipeline=self,
        )
        self.load_lora_into_text_encoder(
            state_dict,
            network_alphas=network_alphas,
            text_encoder=getattr(self, self.text_encoder_name)
            if not hasattr(self, "text_encoder")
            else self.text_encoder,
            lora_scale=self.lora_scale,
            low_cpu_mem_usage=low_cpu_mem_usage,
            adapter_name=adapter_name,
            _pipeline=self,
        )

    @classmethod
    def lora_state_dict(
        cls,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        **kwargs,
    ):
        r"""
        Return state dict and network alphas of the LoRA weights.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                Can be either:

                    - A string, the *model id* (for example `google/ddpm-celebahq-256`) of a pretrained model hosted on
                      the Hub.
                    - A path to a *directory* (for example `./my_model_directory`) containing the model weights.
                    - A [torch state
                      dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).

            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.
            resume_download (`bool`, *optional*, defaults to `False`):
                Whether or not to resume downloading the model weights and configuration files. If set to `False`, any
                incompletely downloaded files are deleted.
            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to `True`, the model
                won't be downloaded from the Hub.
            use_auth_token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            subfolder (`str`, *optional*, defaults to `""`):
                The subfolder location of a model file within a larger model repository on the Hub or locally.
            low_cpu_mem_usage (`bool`, *optional*, defaults to `True` if torch version >= 1.9.0 else `False`):
                Speed up model loading only loading the pretrained weights and not initializing the weights. This also
                tries to not use more than 1x model size in CPU memory (including peak memory) while loading the model.
                Only supported for PyTorch >= 1.9.0. If you are using an older version of PyTorch, setting this
                argument to `True` will raise an error.
            mirror (`str`, *optional*):
                Mirror source to resolve accessibility issues if you're downloading a model in China. We do not
                guarantee the timeliness or safety of the source, and you should refer to the mirror site for more
                information.
        """
        # Load the main state dict first which has the LoRA layers for either of
        # UNet and text encoder or both.
        cache_dir = kwargs.pop("cache_dir", DIFFUSERS_CACHE)
        force_download = kwargs.pop("force_download", False)
        resume_download = kwargs.pop("resume_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", HF_HUB_OFFLINE)
        use_auth_token = kwargs.pop("use_auth_token", None)
        revision = kwargs.pop("revision", None)
        subfolder = kwargs.pop("subfolder", None)
        weight_name = kwargs.pop("weight_name", None)
        unet_config = kwargs.pop("unet_config", None)
        use_safetensors = kwargs.pop("use_safetensors", None)

        allow_pickle = False
        if use_safetensors is None:
            use_safetensors = True
            allow_pickle = True

        user_agent = {
            "file_type": "attn_procs_weights",
            "framework": "pytorch",
        }

        model_file = None
        if not isinstance(pretrained_model_name_or_path_or_dict, dict):
            # Let's first try to load .safetensors weights
            if (use_safetensors and weight_name is None) or (
                weight_name is not None and weight_name.endswith(".safetensors")
            ):
                try:
                    # Here we're relaxing the loading check to enable more Inference API
                    # friendliness where sometimes, it's not at all possible to automatically
                    # determine `weight_name`.
                    if weight_name is None:
                        weight_name = cls._best_guess_weight_name(
                            pretrained_model_name_or_path_or_dict, file_extension=".safetensors"
                        )
                    model_file = _get_model_file(
                        pretrained_model_name_or_path_or_dict,
                        weights_name=weight_name or LORA_WEIGHT_NAME_SAFE,
                        cache_dir=cache_dir,
                        force_download=force_download,
                        resume_download=resume_download,
                        proxies=proxies,
                        local_files_only=local_files_only,
                        use_auth_token=use_auth_token,
                        revision=revision,
                        subfolder=subfolder,
                        user_agent=user_agent,
                    )
                    state_dict = safetensors.torch.load_file(model_file, device="cpu")
                except (IOError, safetensors.SafetensorError) as e:
                    if not allow_pickle:
                        raise e
                    # try loading non-safetensors weights
                    model_file = None
                    pass

            if model_file is None:
                if weight_name is None:
                    weight_name = cls._best_guess_weight_name(
                        pretrained_model_name_or_path_or_dict, file_extension=".bin"
                    )
                model_file = _get_model_file(
                    pretrained_model_name_or_path_or_dict,
                    weights_name=weight_name or LORA_WEIGHT_NAME,
                    cache_dir=cache_dir,
                    force_download=force_download,
                    resume_download=resume_download,
                    proxies=proxies,
                    local_files_only=local_files_only,
                    use_auth_token=use_auth_token,
                    revision=revision,
                    subfolder=subfolder,
                    user_agent=user_agent,
                )
                state_dict = torch.load(model_file, map_location="cpu")
        else:
            state_dict = pretrained_model_name_or_path_or_dict

        network_alphas = None
        # TODO: replace it with a method from `state_dict_utils`
        if all(
            (
                k.startswith("lora_te_")
                or k.startswith("lora_unet_")
                or k.startswith("lora_te1_")
                or k.startswith("lora_te2_")
            )
            for k in state_dict.keys()
        ):
            # Map SDXL blocks correctly.
            if unet_config is not None:
                # use unet config to remap block numbers
                state_dict = cls._maybe_map_sgm_blocks_to_diffusers(state_dict, unet_config)
            state_dict, network_alphas = cls._convert_kohya_lora_to_diffusers(state_dict)

        return state_dict, network_alphas

    @classmethod
    def _best_guess_weight_name(cls, pretrained_model_name_or_path_or_dict, file_extension=".safetensors"):
        targeted_files = []

        if os.path.isfile(pretrained_model_name_or_path_or_dict):
            return
        elif os.path.isdir(pretrained_model_name_or_path_or_dict):
            targeted_files = [
                f for f in os.listdir(pretrained_model_name_or_path_or_dict) if f.endswith(file_extension)
            ]
        else:
            files_in_repo = model_info(pretrained_model_name_or_path_or_dict).siblings
            targeted_files = [f.rfilename for f in files_in_repo if f.rfilename.endswith(file_extension)]
        if len(targeted_files) == 0:
            return

        # "scheduler" does not correspond to a LoRA checkpoint.
        # "optimizer" does not correspond to a LoRA checkpoint
        # only top-level checkpoints are considered and not the other ones, hence "checkpoint".
        unallowed_substrings = {"scheduler", "optimizer", "checkpoint"}
        targeted_files = list(
            filter(lambda x: all(substring not in x for substring in unallowed_substrings), targeted_files)
        )

        if any(f.endswith(LORA_WEIGHT_NAME) for f in targeted_files):
            targeted_files = list(filter(lambda x: x.endswith(LORA_WEIGHT_NAME), targeted_files))
        elif any(f.endswith(LORA_WEIGHT_NAME_SAFE) for f in targeted_files):
            targeted_files = list(filter(lambda x: x.endswith(LORA_WEIGHT_NAME_SAFE), targeted_files))

        if len(targeted_files) > 1:
            raise ValueError(
                f"Provided path contains more than one weights file in the {file_extension} format. Either specify `weight_name` in `load_lora_weights` or make sure there's only one  `.safetensors` or `.bin` file in  {pretrained_model_name_or_path_or_dict}."
            )
        weight_name = targeted_files[0]
        return weight_name

    @classmethod
    def _maybe_map_sgm_blocks_to_diffusers(cls, state_dict, unet_config, delimiter="_", block_slice_pos=5):
        # 1. get all state_dict_keys
        all_keys = list(state_dict.keys())
        sgm_patterns = ["input_blocks", "middle_block", "output_blocks"]

        # 2. check if needs remapping, if not return original dict
        is_in_sgm_format = False
        for key in all_keys:
            if any(p in key for p in sgm_patterns):
                is_in_sgm_format = True
                break

        if not is_in_sgm_format:
            return state_dict

        # 3. Else remap from SGM patterns
        new_state_dict = {}
        inner_block_map = ["resnets", "attentions", "upsamplers"]

        # Retrieves # of down, mid and up blocks
        input_block_ids, middle_block_ids, output_block_ids = set(), set(), set()

        for layer in all_keys:
            if "text" in layer:
                new_state_dict[layer] = state_dict.pop(layer)
            else:
                layer_id = int(layer.split(delimiter)[:block_slice_pos][-1])
                if sgm_patterns[0] in layer:
                    input_block_ids.add(layer_id)
                elif sgm_patterns[1] in layer:
                    middle_block_ids.add(layer_id)
                elif sgm_patterns[2] in layer:
                    output_block_ids.add(layer_id)
                else:
                    raise ValueError(f"Checkpoint not supported because layer {layer} not supported.")

        input_blocks = {
            layer_id: [key for key in state_dict if f"input_blocks{delimiter}{layer_id}" in key]
            for layer_id in input_block_ids
        }
        middle_blocks = {
            layer_id: [key for key in state_dict if f"middle_block{delimiter}{layer_id}" in key]
            for layer_id in middle_block_ids
        }
        output_blocks = {
            layer_id: [key for key in state_dict if f"output_blocks{delimiter}{layer_id}" in key]
            for layer_id in output_block_ids
        }

        # Rename keys accordingly
        for i in input_block_ids:
            block_id = (i - 1) // (unet_config.layers_per_block + 1)
            layer_in_block_id = (i - 1) % (unet_config.layers_per_block + 1)

            for key in input_blocks[i]:
                inner_block_id = int(key.split(delimiter)[block_slice_pos])
                inner_block_key = inner_block_map[inner_block_id] if "op" not in key else "downsamplers"
                inner_layers_in_block = str(layer_in_block_id) if "op" not in key else "0"
                new_key = delimiter.join(
                    key.split(delimiter)[: block_slice_pos - 1]
                    + [str(block_id), inner_block_key, inner_layers_in_block]
                    + key.split(delimiter)[block_slice_pos + 1 :]
                )
                new_state_dict[new_key] = state_dict.pop(key)

        for i in middle_block_ids:
            key_part = None
            if i == 0:
                key_part = [inner_block_map[0], "0"]
            elif i == 1:
                key_part = [inner_block_map[1], "0"]
            elif i == 2:
                key_part = [inner_block_map[0], "1"]
            else:
                raise ValueError(f"Invalid middle block id {i}.")

            for key in middle_blocks[i]:
                new_key = delimiter.join(
                    key.split(delimiter)[: block_slice_pos - 1] + key_part + key.split(delimiter)[block_slice_pos:]
                )
                new_state_dict[new_key] = state_dict.pop(key)

        for i in output_block_ids:
            block_id = i // (unet_config.layers_per_block + 1)
            layer_in_block_id = i % (unet_config.layers_per_block + 1)

            for key in output_blocks[i]:
                inner_block_id = int(key.split(delimiter)[block_slice_pos])
                inner_block_key = inner_block_map[inner_block_id]
                inner_layers_in_block = str(layer_in_block_id) if inner_block_id < 2 else "0"
                new_key = delimiter.join(
                    key.split(delimiter)[: block_slice_pos - 1]
                    + [str(block_id), inner_block_key, inner_layers_in_block]
                    + key.split(delimiter)[block_slice_pos + 1 :]
                )
                new_state_dict[new_key] = state_dict.pop(key)

        if len(state_dict) > 0:
            raise ValueError("At this point all state dict entries have to be converted.")

        return new_state_dict

    @classmethod
    def _optionally_disable_offloading(cls, _pipeline):
        """
        Optionally removes offloading in case the pipeline has been already sequentially offloaded to CPU.

        Args:
            _pipeline (`DiffusionPipeline`):
                The pipeline to disable offloading for.

        Returns:
            tuple:
                A tuple indicating if `is_model_cpu_offload` or `is_sequential_cpu_offload` is True.
        """
        is_model_cpu_offload = False
        is_sequential_cpu_offload = False

        if _pipeline is not None:
            for _, component in _pipeline.components.items():
                if isinstance(component, nn.Module) and hasattr(component, "_hf_hook"):
                    if not is_model_cpu_offload:
                        is_model_cpu_offload = isinstance(component._hf_hook, CpuOffload)
                    if not is_sequential_cpu_offload:
                        is_sequential_cpu_offload = isinstance(component._hf_hook, AlignDevicesHook)

                    logger.info(
                        "Accelerate hooks detected. Since you have called `load_lora_weights()`, the previous hooks will be first removed. Then the LoRA parameters will be loaded and the hooks will be applied again."
                    )
                    remove_hook_from_module(component, recurse=is_sequential_cpu_offload)

        return (is_model_cpu_offload, is_sequential_cpu_offload)

    @classmethod
    def load_lora_into_unet(
        cls, state_dict, network_alphas, unet, low_cpu_mem_usage=None, adapter_name=None, _pipeline=None
    ):
        """
        Load LoRA layers specified in `state_dict` into `unet`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the LoRA layer parameters. The keys can either be indexed directly
                into the `unet` or prefixed with an additional `unet`, which can be used to distinguish between text
                encoder LoRA layers.
            network_alphas (`Dict[str, float]`):
                See
                [`LoRALinearLayer`](https://github.com/huggingface/diffusers/blob/c697f524761abd2314c030221a3ad2f7791eab4e/src/diffusers/models/lora.py#L182)
                for more details.
            unet (`UNet2DConditionModel`):
                The UNet model to load the LoRA layers into.
            low_cpu_mem_usage (`bool`, *optional*, defaults to `True` if torch version >= 1.9.0 else `False`):
                Only load and not initialize the pretrained weights. This can speedup model loading and also tries to
                not use more than 1x model size in CPU memory (including peak memory) while loading the model. Only
                supported for PyTorch >= 1.9.0. If you are using an older version of PyTorch, setting this argument to
                `True` will raise an error.
            adapter_name (`str`, *optional*):
                Name for referencing the loaded adapter model. If not specified, it will use `default_{i}` where `i` is
                the total number of adapters being loaded.
        """
        low_cpu_mem_usage = low_cpu_mem_usage if low_cpu_mem_usage is not None else _LOW_CPU_MEM_USAGE_DEFAULT
        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `cls.unet_name` and/or `cls.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())

        if all(key.startswith(cls.unet_name) or key.startswith(cls.text_encoder_name) for key in keys):
            # Load the layers corresponding to UNet.
            logger.info(f"Loading {cls.unet_name}.")

            unet_keys = [k for k in keys if k.startswith(cls.unet_name)]
            state_dict = {k.replace(f"{cls.unet_name}.", ""): v for k, v in state_dict.items() if k in unet_keys}

            if network_alphas is not None:
                alpha_keys = [k for k in network_alphas.keys() if k.startswith(cls.unet_name)]
                network_alphas = {
                    k.replace(f"{cls.unet_name}.", ""): v for k, v in network_alphas.items() if k in alpha_keys
                }

        else:
            # Otherwise, we're dealing with the old format. This means the `state_dict` should only
            # contain the module names of the `unet` as its keys WITHOUT any prefix.
            warn_message = "You have saved the LoRA weights using the old format. To convert the old LoRA weights to the new format, you can first load them in a dictionary and then create a new dictionary like the following: `new_state_dict = {f'unet.{module_name}': params for module_name, params in old_state_dict.items()}`."
            logger.warn(warn_message)

        if USE_PEFT_BACKEND and len(state_dict.keys()) > 0:
            from peft import LoraConfig, inject_adapter_in_model, set_peft_model_state_dict

            if adapter_name in getattr(unet, "peft_config", {}):
                raise ValueError(
                    f"Adapter name {adapter_name} already in use in the Unet - please select a new adapter name."
                )

            state_dict = convert_unet_state_dict_to_peft(state_dict)

            if network_alphas is not None:
                # The alphas state dict have the same structure as Unet, thus we convert it to peft format using
                # `convert_unet_state_dict_to_peft` method.
                network_alphas = convert_unet_state_dict_to_peft(network_alphas)

            rank = {}
            for key, val in state_dict.items():
                if "lora_B" in key:
                    rank[key] = val.shape[1]

            lora_config_kwargs = get_peft_kwargs(rank, network_alphas, state_dict, is_unet=True)
            lora_config = LoraConfig(**lora_config_kwargs)

            # adapter_name
            if adapter_name is None:
                adapter_name = get_adapter_name(unet)

            # In case the pipeline has been already offloaded to CPU - temporarily remove the hooks
            # otherwise loading LoRA weights will lead to an error
            is_model_cpu_offload, is_sequential_cpu_offload = cls._optionally_disable_offloading(_pipeline)

            inject_adapter_in_model(lora_config, unet, adapter_name=adapter_name)
            incompatible_keys = set_peft_model_state_dict(unet, state_dict, adapter_name)

            if incompatible_keys is not None:
                # check only for unexpected keys
                unexpected_keys = getattr(incompatible_keys, "unexpected_keys", None)
                if unexpected_keys:
                    logger.warning(
                        f"Loading adapter weights from state_dict led to unexpected keys not found in the model: "
                        f" {unexpected_keys}. "
                    )

            # Offload back.
            if is_model_cpu_offload:
                _pipeline.enable_model_cpu_offload()
            elif is_sequential_cpu_offload:
                _pipeline.enable_sequential_cpu_offload()
            # Unsafe code />

        unet.load_attn_procs(
            state_dict, network_alphas=network_alphas, low_cpu_mem_usage=low_cpu_mem_usage, _pipeline=_pipeline
        )

    @classmethod
    def load_lora_into_text_encoder(
        cls,
        state_dict,
        network_alphas,
        text_encoder,
        prefix=None,
        lora_scale=1.0,
        low_cpu_mem_usage=None,
        adapter_name=None,
        _pipeline=None,
    ):
        """
        Load LoRA layers specified in `state_dict` into `text_encoder`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the LoRA layer parameters. The key should be prefixed with an
                additional `text_encoder` to distinguish between UNet LoRA layers.
            network_alphas (`Dict[str, float]`):
                See
                [`LoRALinearLayer`](https://github.com/huggingface/diffusers/blob/c697f524761abd2314c030221a3ad2f7791eab4e/src/diffusers/models/lora.py#L182)
                for more details.
            text_encoder (`CLIPTextModel`):
                The text encoder model to load the LoRA layers into.
            prefix (`str`):
                Expected prefix of the `text_encoder` in the `state_dict`.
            lora_scale (`float`):
                Scale of `LoRALinearLayer`'s output before it is added with the output of the regular LoRA layer.
            low_cpu_mem_usage (`bool`, *optional*, defaults to `True` if torch version >= 1.9.0 else `False`):
                Only load and not initialize the pretrained weights. This can speedup model loading and also tries to
                not use more than 1x model size in CPU memory (including peak memory) while loading the model. Only
                supported for PyTorch >= 1.9.0. If you are using an older version of PyTorch, setting this argument to
                `True` will raise an error.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
        """
        low_cpu_mem_usage = low_cpu_mem_usage if low_cpu_mem_usage is not None else _LOW_CPU_MEM_USAGE_DEFAULT

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `self.unet_name` and/or `self.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        prefix = cls.text_encoder_name if prefix is None else prefix

        # Safe prefix to check with.
        if any(cls.text_encoder_name in key for key in keys):
            # Load the layers corresponding to text encoder and make necessary adjustments.
            text_encoder_keys = [k for k in keys if k.startswith(prefix) and k.split(".")[0] == prefix]
            text_encoder_lora_state_dict = {
                k.replace(f"{prefix}.", ""): v for k, v in state_dict.items() if k in text_encoder_keys
            }

            if len(text_encoder_lora_state_dict) > 0:
                logger.info(f"Loading {prefix}.")
                rank = {}
                text_encoder_lora_state_dict = convert_state_dict_to_diffusers(text_encoder_lora_state_dict)

                if USE_PEFT_BACKEND:
                    # convert state dict
                    text_encoder_lora_state_dict = convert_state_dict_to_peft(text_encoder_lora_state_dict)

                    for name, _ in text_encoder_attn_modules(text_encoder):
                        rank_key = f"{name}.out_proj.lora_B.weight"
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                    patch_mlp = any(".mlp." in key for key in text_encoder_lora_state_dict.keys())
                    if patch_mlp:
                        for name, _ in text_encoder_mlp_modules(text_encoder):
                            rank_key_fc1 = f"{name}.fc1.lora_B.weight"
                            rank_key_fc2 = f"{name}.fc2.lora_B.weight"

                            rank[rank_key_fc1] = text_encoder_lora_state_dict[rank_key_fc1].shape[1]
                            rank[rank_key_fc2] = text_encoder_lora_state_dict[rank_key_fc2].shape[1]
                else:
                    for name, _ in text_encoder_attn_modules(text_encoder):
                        rank_key = f"{name}.out_proj.lora_linear_layer.up.weight"
                        rank.update({rank_key: text_encoder_lora_state_dict[rank_key].shape[1]})

                    patch_mlp = any(".mlp." in key for key in text_encoder_lora_state_dict.keys())
                    if patch_mlp:
                        for name, _ in text_encoder_mlp_modules(text_encoder):
                            rank_key_fc1 = f"{name}.fc1.lora_linear_layer.up.weight"
                            rank_key_fc2 = f"{name}.fc2.lora_linear_layer.up.weight"
                            rank[rank_key_fc1] = text_encoder_lora_state_dict[rank_key_fc1].shape[1]
                            rank[rank_key_fc2] = text_encoder_lora_state_dict[rank_key_fc2].shape[1]

                if network_alphas is not None:
                    alpha_keys = [
                        k for k in network_alphas.keys() if k.startswith(prefix) and k.split(".")[0] == prefix
                    ]
                    network_alphas = {
                        k.replace(f"{prefix}.", ""): v for k, v in network_alphas.items() if k in alpha_keys
                    }

                if USE_PEFT_BACKEND:
                    from peft import LoraConfig

                    lora_config_kwargs = get_peft_kwargs(
                        rank, network_alphas, text_encoder_lora_state_dict, is_unet=False
                    )

                    lora_config = LoraConfig(**lora_config_kwargs)

                    # adapter_name
                    if adapter_name is None:
                        adapter_name = get_adapter_name(text_encoder)

                    is_model_cpu_offload, is_sequential_cpu_offload = cls._optionally_disable_offloading(_pipeline)

                    # inject LoRA layers and load the state dict
                    # in transformers we automatically check whether the adapter name is already in use or not
                    text_encoder.load_adapter(
                        adapter_name=adapter_name,
                        adapter_state_dict=text_encoder_lora_state_dict,
                        peft_config=lora_config,
                    )

                    # scale LoRA layers with `lora_scale`
                    scale_lora_layers(text_encoder, weight=lora_scale)
                else:
                    cls._modify_text_encoder(
                        text_encoder,
                        lora_scale,
                        network_alphas,
                        rank=rank,
                        patch_mlp=patch_mlp,
                        low_cpu_mem_usage=low_cpu_mem_usage,
                    )

                    is_pipeline_offloaded = _pipeline is not None and any(
                        isinstance(c, torch.nn.Module) and hasattr(c, "_hf_hook")
                        for c in _pipeline.components.values()
                    )
                    if is_pipeline_offloaded and low_cpu_mem_usage:
                        low_cpu_mem_usage = True
                        logger.info(
                            f"Pipeline {_pipeline.__class__} is offloaded. Therefore low cpu mem usage loading is forced."
                        )

                    if low_cpu_mem_usage:
                        device = next(iter(text_encoder_lora_state_dict.values())).device
                        dtype = next(iter(text_encoder_lora_state_dict.values())).dtype
                        unexpected_keys = load_model_dict_into_meta(
                            text_encoder, text_encoder_lora_state_dict, device=device, dtype=dtype
                        )
                    else:
                        load_state_dict_results = text_encoder.load_state_dict(
                            text_encoder_lora_state_dict, strict=False
                        )
                        unexpected_keys = load_state_dict_results.unexpected_keys

                    if len(unexpected_keys) != 0:
                        raise ValueError(
                            f"failed to load text encoder state dict, unexpected keys: {load_state_dict_results.unexpected_keys}"
                        )

                    # <Unsafe code
                    # We can be sure that the following works as all we do is change the dtype and device of the text encoder
                    # Now we remove any existing hooks to
                    is_model_cpu_offload = False
                    is_sequential_cpu_offload = False
                    if _pipeline is not None:
                        for _, component in _pipeline.components.items():
                            if isinstance(component, torch.nn.Module):
                                if hasattr(component, "_hf_hook"):
                                    is_model_cpu_offload = isinstance(getattr(component, "_hf_hook"), CpuOffload)
                                    is_sequential_cpu_offload = isinstance(
                                        getattr(component, "_hf_hook"), AlignDevicesHook
                                    )
                                    logger.info(
                                        "Accelerate hooks detected. Since you have called `load_lora_weights()`, the previous hooks will be first removed. Then the LoRA parameters will be loaded and the hooks will be applied again."
                                    )
                                    remove_hook_from_module(component, recurse=is_sequential_cpu_offload)

                text_encoder.to(device=text_encoder.device, dtype=text_encoder.dtype)

                # Offload back.
                if is_model_cpu_offload:
                    _pipeline.enable_model_cpu_offload()
                elif is_sequential_cpu_offload:
                    _pipeline.enable_sequential_cpu_offload()
                # Unsafe code />

    @property
    def lora_scale(self) -> float:
        # property function that returns the lora scale which can be set at run time by the pipeline.
        # if _lora_scale has not been set, return 1
        return self._lora_scale if hasattr(self, "_lora_scale") else 1.0

    def _remove_text_encoder_monkey_patch(self):
        if USE_PEFT_BACKEND:
            remove_method = recurse_remove_peft_layers
        else:
            remove_method = self._remove_text_encoder_monkey_patch_classmethod

        if hasattr(self, "text_encoder"):
            remove_method(self.text_encoder)

            # In case text encoder have no Lora attached
            if USE_PEFT_BACKEND and getattr(self.text_encoder, "peft_config", None) is not None:
                del self.text_encoder.peft_config
                self.text_encoder._hf_peft_config_loaded = None
        if hasattr(self, "text_encoder_2"):
            remove_method(self.text_encoder_2)
            if USE_PEFT_BACKEND:
                del self.text_encoder_2.peft_config
                self.text_encoder_2._hf_peft_config_loaded = None

    @classmethod
    def _remove_text_encoder_monkey_patch_classmethod(cls, text_encoder):
        if version.parse(__version__) > version.parse("0.23"):
            deprecate("_remove_text_encoder_monkey_patch_classmethod", "0.25", LORA_DEPRECATION_MESSAGE)

        for _, attn_module in text_encoder_attn_modules(text_encoder):
            if isinstance(attn_module.q_proj, PatchedLoraProjection):
                attn_module.q_proj.lora_linear_layer = None
                attn_module.k_proj.lora_linear_layer = None
                attn_module.v_proj.lora_linear_layer = None
                attn_module.out_proj.lora_linear_layer = None

        for _, mlp_module in text_encoder_mlp_modules(text_encoder):
            if isinstance(mlp_module.fc1, PatchedLoraProjection):
                mlp_module.fc1.lora_linear_layer = None
                mlp_module.fc2.lora_linear_layer = None

    @classmethod
    def _modify_text_encoder(
        cls,
        text_encoder,
        lora_scale=1,
        network_alphas=None,
        rank: Union[Dict[str, int], int] = 4,
        dtype=None,
        patch_mlp=False,
        low_cpu_mem_usage=False,
    ):
        r"""
        Monkey-patches the forward passes of attention modules of the text encoder.
        """
        if version.parse(__version__) > version.parse("0.23"):
            deprecate("_modify_text_encoder", "0.25", LORA_DEPRECATION_MESSAGE)

        def create_patched_linear_lora(model, network_alpha, rank, dtype, lora_parameters):
            linear_layer = model.regular_linear_layer if isinstance(model, PatchedLoraProjection) else model
            ctx = init_empty_weights if low_cpu_mem_usage else nullcontext
            with ctx():
                model = PatchedLoraProjection(linear_layer, lora_scale, network_alpha, rank, dtype=dtype)

            lora_parameters.extend(model.lora_linear_layer.parameters())
            return model

        # First, remove any monkey-patch that might have been applied before
        cls._remove_text_encoder_monkey_patch_classmethod(text_encoder)

        lora_parameters = []
        network_alphas = {} if network_alphas is None else network_alphas
        is_network_alphas_populated = len(network_alphas) > 0

        for name, attn_module in text_encoder_attn_modules(text_encoder):
            query_alpha = network_alphas.pop(name + ".to_q_lora.down.weight.alpha", None)
            key_alpha = network_alphas.pop(name + ".to_k_lora.down.weight.alpha", None)
            value_alpha = network_alphas.pop(name + ".to_v_lora.down.weight.alpha", None)
            out_alpha = network_alphas.pop(name + ".to_out_lora.down.weight.alpha", None)

            if isinstance(rank, dict):
                current_rank = rank.pop(f"{name}.out_proj.lora_linear_layer.up.weight")
            else:
                current_rank = rank

            attn_module.q_proj = create_patched_linear_lora(
                attn_module.q_proj, query_alpha, current_rank, dtype, lora_parameters
            )
            attn_module.k_proj = create_patched_linear_lora(
                attn_module.k_proj, key_alpha, current_rank, dtype, lora_parameters
            )
            attn_module.v_proj = create_patched_linear_lora(
                attn_module.v_proj, value_alpha, current_rank, dtype, lora_parameters
            )
            attn_module.out_proj = create_patched_linear_lora(
                attn_module.out_proj, out_alpha, current_rank, dtype, lora_parameters
            )

        if patch_mlp:
            for name, mlp_module in text_encoder_mlp_modules(text_encoder):
                fc1_alpha = network_alphas.pop(name + ".fc1.lora_linear_layer.down.weight.alpha", None)
                fc2_alpha = network_alphas.pop(name + ".fc2.lora_linear_layer.down.weight.alpha", None)

                current_rank_fc1 = rank.pop(f"{name}.fc1.lora_linear_layer.up.weight")
                current_rank_fc2 = rank.pop(f"{name}.fc2.lora_linear_layer.up.weight")

                mlp_module.fc1 = create_patched_linear_lora(
                    mlp_module.fc1, fc1_alpha, current_rank_fc1, dtype, lora_parameters
                )
                mlp_module.fc2 = create_patched_linear_lora(
                    mlp_module.fc2, fc2_alpha, current_rank_fc2, dtype, lora_parameters
                )

        if is_network_alphas_populated and len(network_alphas) > 0:
            raise ValueError(
                f"The `network_alphas` has to be empty at this point but has the following keys \n\n {', '.join(network_alphas.keys())}"
            )

        return lora_parameters

    @classmethod
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        unet_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_lora_layers: Dict[str, torch.nn.Module] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the UNet and text encoder LoRA parameters.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to (will be created if it doesn't exist).
            unet_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `unet`.
            text_encoder_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dict. Useful during distributed training when you need to replace
                `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or with `pickle`.

        Example:

        ```py
        from diffusers import StableDiffusionXLPipeline
        from peft.utils import get_peft_model_state_dict
        import torch

        pipeline = StableDiffusionXLPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora()

        # get and save unet state dict
        unet_state_dict = get_peft_model_state_dict(pipeline.unet, adapter_name="pixel")
        pipeline.save_lora_weights("fused-model", unet_lora_layers=unet_state_dict)
        pipeline.load_lora_weights("fused-model", weight_name="pytorch_lora_weights.safetensors")
        ```
        """
        # Create a flat dictionary.
        state_dict = {}

        # Populate the dictionary.
        if unet_lora_layers is not None:
            weights = (
                unet_lora_layers.state_dict() if isinstance(unet_lora_layers, torch.nn.Module) else unet_lora_layers
            )

            unet_lora_state_dict = {f"{cls.unet_name}.{module_name}": param for module_name, param in weights.items()}
            state_dict.update(unet_lora_state_dict)

        if text_encoder_lora_layers is not None:
            weights = (
                text_encoder_lora_layers.state_dict()
                if isinstance(text_encoder_lora_layers, torch.nn.Module)
                else text_encoder_lora_layers
            )

            text_encoder_lora_state_dict = {
                f"{cls.text_encoder_name}.{module_name}": param for module_name, param in weights.items()
            }
            state_dict.update(text_encoder_lora_state_dict)

        # Save the model
        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )

    @staticmethod
    def write_lora_layers(
        state_dict: Dict[str, torch.Tensor],
        save_directory: str,
        is_main_process: bool,
        weight_name: str,
        save_function: Callable,
        safe_serialization: bool,
    ):
        if os.path.isfile(save_directory):
            logger.error(f"Provided path ({save_directory}) should be a directory, not a file")
            return

        if save_function is None:
            if safe_serialization:

                def save_function(weights, filename):
                    return safetensors.torch.save_file(weights, filename, metadata={"format": "pt"})

            else:
                save_function = torch.save

        os.makedirs(save_directory, exist_ok=True)

        if weight_name is None:
            if safe_serialization:
                weight_name = LORA_WEIGHT_NAME_SAFE
            else:
                weight_name = LORA_WEIGHT_NAME

        save_function(state_dict, os.path.join(save_directory, weight_name))
        logger.info(f"Model weights saved in {os.path.join(save_directory, weight_name)}")

    @classmethod
    def _convert_kohya_lora_to_diffusers(cls, state_dict):
        unet_state_dict = {}
        te_state_dict = {}
        te2_state_dict = {}
        network_alphas = {}

        # every down weight has a corresponding up weight and potentially an alpha weight
        lora_keys = [k for k in state_dict.keys() if k.endswith("lora_down.weight")]
        for key in lora_keys:
            lora_name = key.split(".")[0]
            lora_name_up = lora_name + ".lora_up.weight"
            lora_name_alpha = lora_name + ".alpha"

            if lora_name.startswith("lora_unet_"):
                diffusers_name = key.replace("lora_unet_", "").replace("_", ".")

                if "input.blocks" in diffusers_name:
                    diffusers_name = diffusers_name.replace("input.blocks", "down_blocks")
                else:
                    diffusers_name = diffusers_name.replace("down.blocks", "down_blocks")

                if "middle.block" in diffusers_name:
                    diffusers_name = diffusers_name.replace("middle.block", "mid_block")
                else:
                    diffusers_name = diffusers_name.replace("mid.block", "mid_block")
                if "output.blocks" in diffusers_name:
                    diffusers_name = diffusers_name.replace("output.blocks", "up_blocks")
                else:
                    diffusers_name = diffusers_name.replace("up.blocks", "up_blocks")

                diffusers_name = diffusers_name.replace("transformer.blocks", "transformer_blocks")
                diffusers_name = diffusers_name.replace("to.q.lora", "to_q_lora")
                diffusers_name = diffusers_name.replace("to.k.lora", "to_k_lora")
                diffusers_name = diffusers_name.replace("to.v.lora", "to_v_lora")
                diffusers_name = diffusers_name.replace("to.out.0.lora", "to_out_lora")
                diffusers_name = diffusers_name.replace("proj.in", "proj_in")
                diffusers_name = diffusers_name.replace("proj.out", "proj_out")
                diffusers_name = diffusers_name.replace("emb.layers", "time_emb_proj")

                # SDXL specificity.
                if "emb" in diffusers_name and "time.emb.proj" not in diffusers_name:
                    pattern = r"\.\d+(?=\D*$)"
                    diffusers_name = re.sub(pattern, "", diffusers_name, count=1)
                if ".in." in diffusers_name:
                    diffusers_name = diffusers_name.replace("in.layers.2", "conv1")
                if ".out." in diffusers_name:
                    diffusers_name = diffusers_name.replace("out.layers.3", "conv2")
                if "downsamplers" in diffusers_name or "upsamplers" in diffusers_name:
                    diffusers_name = diffusers_name.replace("op", "conv")
                if "skip" in diffusers_name:
                    diffusers_name = diffusers_name.replace("skip.connection", "conv_shortcut")

                # LyCORIS specificity.
                if "time.emb.proj" in diffusers_name:
                    diffusers_name = diffusers_name.replace("time.emb.proj", "time_emb_proj")
                if "conv.shortcut" in diffusers_name:
                    diffusers_name = diffusers_name.replace("conv.shortcut", "conv_shortcut")

                # General coverage.
                if "transformer_blocks" in diffusers_name:
                    if "attn1" in diffusers_name or "attn2" in diffusers_name:
                        diffusers_name = diffusers_name.replace("attn1", "attn1.processor")
                        diffusers_name = diffusers_name.replace("attn2", "attn2.processor")
                        unet_state_dict[diffusers_name] = state_dict.pop(key)
                        unet_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)
                    elif "ff" in diffusers_name:
                        unet_state_dict[diffusers_name] = state_dict.pop(key)
                        unet_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)
                elif any(key in diffusers_name for key in ("proj_in", "proj_out")):
                    unet_state_dict[diffusers_name] = state_dict.pop(key)
                    unet_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)
                else:
                    unet_state_dict[diffusers_name] = state_dict.pop(key)
                    unet_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)

            elif lora_name.startswith("lora_te_"):
                diffusers_name = key.replace("lora_te_", "").replace("_", ".")
                diffusers_name = diffusers_name.replace("text.model", "text_model")
                diffusers_name = diffusers_name.replace("self.attn", "self_attn")
                diffusers_name = diffusers_name.replace("q.proj.lora", "to_q_lora")
                diffusers_name = diffusers_name.replace("k.proj.lora", "to_k_lora")
                diffusers_name = diffusers_name.replace("v.proj.lora", "to_v_lora")
                diffusers_name = diffusers_name.replace("out.proj.lora", "to_out_lora")
                if "self_attn" in diffusers_name:
                    te_state_dict[diffusers_name] = state_dict.pop(key)
                    te_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)
                elif "mlp" in diffusers_name:
                    # Be aware that this is the new diffusers convention and the rest of the code might
                    # not utilize it yet.
                    diffusers_name = diffusers_name.replace(".lora.", ".lora_linear_layer.")
                    te_state_dict[diffusers_name] = state_dict.pop(key)
                    te_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)

            # (sayakpaul): Duplicate code. Needs to be cleaned.
            elif lora_name.startswith("lora_te1_"):
                diffusers_name = key.replace("lora_te1_", "").replace("_", ".")
                diffusers_name = diffusers_name.replace("text.model", "text_model")
                diffusers_name = diffusers_name.replace("self.attn", "self_attn")
                diffusers_name = diffusers_name.replace("q.proj.lora", "to_q_lora")
                diffusers_name = diffusers_name.replace("k.proj.lora", "to_k_lora")
                diffusers_name = diffusers_name.replace("v.proj.lora", "to_v_lora")
                diffusers_name = diffusers_name.replace("out.proj.lora", "to_out_lora")
                if "self_attn" in diffusers_name:
                    te_state_dict[diffusers_name] = state_dict.pop(key)
                    te_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)
                elif "mlp" in diffusers_name:
                    # Be aware that this is the new diffusers convention and the rest of the code might
                    # not utilize it yet.
                    diffusers_name = diffusers_name.replace(".lora.", ".lora_linear_layer.")
                    te_state_dict[diffusers_name] = state_dict.pop(key)
                    te_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)

            # (sayakpaul): Duplicate code. Needs to be cleaned.
            elif lora_name.startswith("lora_te2_"):
                diffusers_name = key.replace("lora_te2_", "").replace("_", ".")
                diffusers_name = diffusers_name.replace("text.model", "text_model")
                diffusers_name = diffusers_name.replace("self.attn", "self_attn")
                diffusers_name = diffusers_name.replace("q.proj.lora", "to_q_lora")
                diffusers_name = diffusers_name.replace("k.proj.lora", "to_k_lora")
                diffusers_name = diffusers_name.replace("v.proj.lora", "to_v_lora")
                diffusers_name = diffusers_name.replace("out.proj.lora", "to_out_lora")
                if "self_attn" in diffusers_name:
                    te2_state_dict[diffusers_name] = state_dict.pop(key)
                    te2_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)
                elif "mlp" in diffusers_name:
                    # Be aware that this is the new diffusers convention and the rest of the code might
                    # not utilize it yet.
                    diffusers_name = diffusers_name.replace(".lora.", ".lora_linear_layer.")
                    te2_state_dict[diffusers_name] = state_dict.pop(key)
                    te2_state_dict[diffusers_name.replace(".down.", ".up.")] = state_dict.pop(lora_name_up)

            # Rename the alphas so that they can be mapped appropriately.
            if lora_name_alpha in state_dict:
                alpha = state_dict.pop(lora_name_alpha).item()
                if lora_name_alpha.startswith("lora_unet_"):
                    prefix = "unet."
                elif lora_name_alpha.startswith(("lora_te_", "lora_te1_")):
                    prefix = "text_encoder."
                else:
                    prefix = "text_encoder_2."
                new_name = prefix + diffusers_name.split(".lora.")[0] + ".alpha"
                network_alphas.update({new_name: alpha})

        if len(state_dict) > 0:
            raise ValueError(
                f"The following keys have not been correctly be renamed: \n\n {', '.join(state_dict.keys())}"
            )

        logger.info("Kohya-style checkpoint detected.")
        unet_state_dict = {f"{cls.unet_name}.{module_name}": params for module_name, params in unet_state_dict.items()}
        te_state_dict = {
            f"{cls.text_encoder_name}.{module_name}": params for module_name, params in te_state_dict.items()
        }
        te2_state_dict = (
            {f"text_encoder_2.{module_name}": params for module_name, params in te2_state_dict.items()}
            if len(te2_state_dict) > 0
            else None
        )
        if te2_state_dict is not None:
            te_state_dict.update(te2_state_dict)

        new_state_dict = {**unet_state_dict, **te_state_dict}
        return new_state_dict, network_alphas

    def unload_lora_weights(self):
        """
        Unload the LoRA parameters from a pipeline.

        Examples:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.unload_lora_weights()
        ```
        """
        if not USE_PEFT_BACKEND:
            if version.parse(__version__) > version.parse("0.23"):
                logger.warn(
                    "You are using `unload_lora_weights` to disable and unload lora weights. If you want to iteratively enable and disable adapter weights,"
                    "you can use `pipe.enable_lora()` or `pipe.disable_lora()`. After installing the latest version of PEFT."
                )

            for _, module in self.unet.named_modules():
                if hasattr(module, "set_lora_layer"):
                    module.set_lora_layer(None)
        else:
            recurse_remove_peft_layers(self.unet)
            if hasattr(self.unet, "peft_config"):
                del self.unet.peft_config

        # Safe to call the following regardless of LoRA.
        self._remove_text_encoder_monkey_patch()

    def fuse_lora(
        self,
        fuse_unet: bool = True,
        fuse_text_encoder: bool = True,
        lora_scale: float = 1.0,
        safe_fusing: bool = False,
    ):
        r"""
        Fuse the LoRA parameters with the original parameters in their corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            fuse_unet (`bool`, defaults to `True`): Whether to fuse the UNet LoRA parameters.
            fuse_text_encoder (`bool`, defaults to `True`):
                Whether to fuse the text encoder LoRA parameters. If the text encoder wasn't monkey-patched with the
                LoRA parameters then it won't have any effect.
            lora_scale (`float`, defaults to 1.0):
                Controls LoRA influence on the outputs.
            safe_fusing (`bool`, defaults to `False`):
                Whether to check fused weights for `NaN` values before fusing and if values are `NaN`, then don't fuse
                them.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        ```
        """
        if fuse_unet or fuse_text_encoder:
            self.num_fused_loras += 1
            if self.num_fused_loras > 1:
                logger.warn(
                    "The current API is supported for operating with a single LoRA file. You are trying to load and fuse more than one LoRA which is not well-supported.",
                )

        if fuse_unet:
            self.unet.fuse_lora(lora_scale, safe_fusing=safe_fusing)

        if USE_PEFT_BACKEND:
            from peft.tuners.tuners_utils import BaseTunerLayer

            def fuse_text_encoder_lora(text_encoder, lora_scale=1.0, safe_fusing=False):
                # TODO(Patrick, Younes): enable "safe" fusing
                for module in text_encoder.modules():
                    if isinstance(module, BaseTunerLayer):
                        if lora_scale != 1.0:
                            module.scale_layer(lora_scale)

                        module.merge()

        else:
            if version.parse(__version__) > version.parse("0.23"):
                deprecate("fuse_text_encoder_lora", "0.25", LORA_DEPRECATION_MESSAGE)

            def fuse_text_encoder_lora(text_encoder, lora_scale=1.0, safe_fusing=False):
                for _, attn_module in text_encoder_attn_modules(text_encoder):
                    if isinstance(attn_module.q_proj, PatchedLoraProjection):
                        attn_module.q_proj._fuse_lora(lora_scale, safe_fusing)
                        attn_module.k_proj._fuse_lora(lora_scale, safe_fusing)
                        attn_module.v_proj._fuse_lora(lora_scale, safe_fusing)
                        attn_module.out_proj._fuse_lora(lora_scale, safe_fusing)

                for _, mlp_module in text_encoder_mlp_modules(text_encoder):
                    if isinstance(mlp_module.fc1, PatchedLoraProjection):
                        mlp_module.fc1._fuse_lora(lora_scale, safe_fusing)
                        mlp_module.fc2._fuse_lora(lora_scale, safe_fusing)

        if fuse_text_encoder:
            if hasattr(self, "text_encoder"):
                fuse_text_encoder_lora(self.text_encoder, lora_scale, safe_fusing)
            if hasattr(self, "text_encoder_2"):
                fuse_text_encoder_lora(self.text_encoder_2, lora_scale, safe_fusing)

    def unfuse_lora(self, unfuse_unet: bool = True, unfuse_text_encoder: bool = True):
        r"""
        Unfuse the LoRA parameters from the original parameters in their corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            unfuse_unet (`bool`, defaults to `True`): Whether to unfuse the UNet LoRA parameters.
            unfuse_text_encoder (`bool`, defaults to `True`):
                Whether to unfuse the text encoder LoRA parameters. If the text encoder wasn't monkey-patched with the
                LoRA parameters then it won't have any effect.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        pipeline.unfuse_lora()
        ```
        """
        if unfuse_unet:
            if not USE_PEFT_BACKEND:
                self.unet.unfuse_lora()
            else:
                from peft.tuners.tuners_utils import BaseTunerLayer

                for module in self.unet.modules():
                    if isinstance(module, BaseTunerLayer):
                        module.unmerge()

        if USE_PEFT_BACKEND:
            from peft.tuners.tuners_utils import BaseTunerLayer

            def unfuse_text_encoder_lora(text_encoder):
                for module in text_encoder.modules():
                    if isinstance(module, BaseTunerLayer):
                        module.unmerge()

        else:
            if version.parse(__version__) > version.parse("0.23"):
                deprecate("unfuse_text_encoder_lora", "0.25", LORA_DEPRECATION_MESSAGE)

            def unfuse_text_encoder_lora(text_encoder):
                for _, attn_module in text_encoder_attn_modules(text_encoder):
                    if isinstance(attn_module.q_proj, PatchedLoraProjection):
                        attn_module.q_proj._unfuse_lora()
                        attn_module.k_proj._unfuse_lora()
                        attn_module.v_proj._unfuse_lora()
                        attn_module.out_proj._unfuse_lora()

                for _, mlp_module in text_encoder_mlp_modules(text_encoder):
                    if isinstance(mlp_module.fc1, PatchedLoraProjection):
                        mlp_module.fc1._unfuse_lora()
                        mlp_module.fc2._unfuse_lora()

        if unfuse_text_encoder:
            if hasattr(self, "text_encoder"):
                unfuse_text_encoder_lora(self.text_encoder)
            if hasattr(self, "text_encoder_2"):
                unfuse_text_encoder_lora(self.text_encoder_2)

        self.num_fused_loras -= 1

    def set_adapters_for_text_encoder(
        self,
        adapter_names: Union[List[str], str],
        text_encoder: Optional["PreTrainedModel"] = None,  # noqa: F821
        text_encoder_weights: List[float] = None,
    ):
        """
        Set the currently active adapter for use in the text encoder.

        Args:
            adapter_names (`List[str]` or `str`):
                The adapter to activate.
            text_encoder (`torch.nn.Module`, *optional*):
                The text encoder module to activate the adapter layers for. If `None`, it will try to get the
                `text_encoder` attribute.
            text_encoder_weights (`List[float]`, *optional*):
                The weights to use for the text encoder. If `None`, the weights are set to `1.0` for all the adapters.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.load_lora_weights(
            "jbilcke-hf/sdxl-cinematic-1", weight_name="pytorch_lora_weights.safetensors", adapter_name="cinematic"
        )
        pipeline.set_adapters_for_text_encoder("pixel")
        ```
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        def process_weights(adapter_names, weights):
            if weights is None:
                weights = [1.0] * len(adapter_names)
            elif isinstance(weights, float):
                weights = [weights]

            if len(adapter_names) != len(weights):
                raise ValueError(
                    f"Length of adapter names {len(adapter_names)} is not equal to the length of the weights {len(weights)}"
                )
            return weights

        adapter_names = [adapter_names] if isinstance(adapter_names, str) else adapter_names
        text_encoder_weights = process_weights(adapter_names, text_encoder_weights)
        text_encoder = text_encoder or getattr(self, "text_encoder", None)
        if text_encoder is None:
            raise ValueError(
                "The pipeline does not have a default `pipe.text_encoder` class. Please make sure to pass a `text_encoder` instead."
            )
        set_weights_and_activate_adapters(text_encoder, adapter_names, text_encoder_weights)

    def disable_lora_for_text_encoder(self, text_encoder: Optional["PreTrainedModel"] = None):  # noqa: F821
        """
        Disable the text encoder's LoRA layers.

        Args:
            text_encoder (`torch.nn.Module`, *optional*):
                The text encoder module to disable the LoRA layers for. If `None`, it will try to get the
                `text_encoder` attribute.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.disable_lora_for_text_encoder()
        ```
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        text_encoder = text_encoder or getattr(self, "text_encoder", None)
        if text_encoder is None:
            raise ValueError("Text Encoder not found.")
        set_adapter_layers(text_encoder, enabled=False)

    def enable_lora_for_text_encoder(self, text_encoder: Optional["PreTrainedModel"] = None):  # noqa: F821
        """
        Enables the text encoder's LoRA layers.

        Args:
            text_encoder (`torch.nn.Module`, *optional*):
                The text encoder module to enable the LoRA layers for. If `None`, it will try to get the `text_encoder`
                attribute.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.enable_lora_for_text_encoder()
        ```
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")
        text_encoder = text_encoder or getattr(self, "text_encoder", None)
        if text_encoder is None:
            raise ValueError("Text Encoder not found.")
        set_adapter_layers(self.text_encoder, enabled=True)

    def set_adapters(
        self,
        adapter_names: Union[List[str], str],
        adapter_weights: Optional[List[float]] = None,
    ):
        # Handle the UNET
        self.unet.set_adapters(adapter_names, adapter_weights)

        # Handle the Text Encoder
        if hasattr(self, "text_encoder"):
            self.set_adapters_for_text_encoder(adapter_names, self.text_encoder, adapter_weights)
        if hasattr(self, "text_encoder_2"):
            self.set_adapters_for_text_encoder(adapter_names, self.text_encoder_2, adapter_weights)

    def disable_lora(self):
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        # Disable unet adapters
        self.unet.disable_lora()

        # Disable text encoder adapters
        if hasattr(self, "text_encoder"):
            self.disable_lora_for_text_encoder(self.text_encoder)
        if hasattr(self, "text_encoder_2"):
            self.disable_lora_for_text_encoder(self.text_encoder_2)

    def enable_lora(self):
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        # Enable unet adapters
        self.unet.enable_lora()

        # Enable text encoder adapters
        if hasattr(self, "text_encoder"):
            self.enable_lora_for_text_encoder(self.text_encoder)
        if hasattr(self, "text_encoder_2"):
            self.enable_lora_for_text_encoder(self.text_encoder_2)

    def delete_adapters(self, adapter_names: Union[List[str], str]):
        """
        Delete an adapter's LoRA layers from the UNet and text encoder(s).

        Args:
            adapter_names (`Union[List[str], str]`):
                The names (single string or list of strings) of the adapter to delete.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.delete_adapters("pixel")
        ```
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        if isinstance(adapter_names, str):
            adapter_names = [adapter_names]

        # Delete unet adapters
        self.unet.delete_adapters(adapter_names)

        for adapter_name in adapter_names:
            # Delete text encoder adapters
            if hasattr(self, "text_encoder"):
                delete_adapter_layers(self.text_encoder, adapter_name)
            if hasattr(self, "text_encoder_2"):
                delete_adapter_layers(self.text_encoder_2, adapter_name)

    def get_active_adapters(self) -> List[str]:
        """
        Get a list of currently active adapters.

        Example:

        ```python
        from diffusers import DiffusionPipeline

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
        ).to("cuda")
        pipeline.load_lora_weights("CiroN2022/toy-face", weight_name="toy_face_sdxl.safetensors", adapter_name="toy")
        pipeline.get_active_adapters()
        ```
        """
        if not USE_PEFT_BACKEND:
            raise ValueError(
                "PEFT backend is required for this method. Please install the latest version of PEFT `pip install -U peft`"
            )

        from peft.tuners.tuners_utils import BaseTunerLayer

        active_adapters = []

        for module in self.unet.modules():
            if isinstance(module, BaseTunerLayer):
                active_adapters = module.active_adapters
                break

        return active_adapters

    def get_list_adapters(self) -> Dict[str, List[str]]:
        """
        Get a list of all currently available adapters for each component in the pipeline.

        Example:

        ```py
        from diffusers import DiffusionPipeline

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
        ).to("cuda")
        pipeline.load_lora_weights(
            "jbilcke-hf/sdxl-cinematic-1", weight_name="pytorch_lora_weights.safetensors", adapter_name="cinematic"
        )
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.get_list_adapters()
        ```
        """
        if not USE_PEFT_BACKEND:
            raise ValueError(
                "PEFT backend is required for this method. Please install the latest version of PEFT `pip install -U peft`"
            )

        set_adapters = {}

        if hasattr(self, "text_encoder") and hasattr(self.text_encoder, "peft_config"):
            set_adapters["text_encoder"] = list(self.text_encoder.peft_config.keys())

        if hasattr(self, "text_encoder_2") and hasattr(self.text_encoder_2, "peft_config"):
            set_adapters["text_encoder_2"] = list(self.text_encoder_2.peft_config.keys())

        if hasattr(self, "unet") and hasattr(self.unet, "peft_config"):
            set_adapters["unet"] = list(self.unet.peft_config.keys())

        return set_adapters

    def set_lora_device(self, adapter_names: List[str], device: Union[torch.device, str, int]) -> None:
        """
        Move a LoRA to a target device. Useful for offloading a LoRA to the CPU in case you want to load multiple
        adapters and free some GPU memory.

        Args:
            adapter_names (`List[str]`):
                List of adapters to send to device.
            device (`Union[torch.device, str, int]`):
                Device (can be a `torch.device`, `str` or `int`) to place adapters on.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.set_lora_device(["pixel"], device="cuda")
        ```
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        from peft.tuners.tuners_utils import BaseTunerLayer

        # Handle the UNET
        for unet_module in self.unet.modules():
            if isinstance(unet_module, BaseTunerLayer):
                for adapter_name in adapter_names:
                    unet_module.lora_A[adapter_name].to(device)
                    unet_module.lora_B[adapter_name].to(device)

        # Handle the text encoder
        modules_to_process = []
        if hasattr(self, "text_encoder"):
            modules_to_process.append(self.text_encoder)

        if hasattr(self, "text_encoder_2"):
            modules_to_process.append(self.text_encoder_2)

        for text_encoder in modules_to_process:
            # loop over submodules
            for text_encoder_module in text_encoder.modules():
                if isinstance(text_encoder_module, BaseTunerLayer):
                    for adapter_name in adapter_names:
                        text_encoder_module.lora_A[adapter_name].to(device)
                        text_encoder_module.lora_B[adapter_name].to(device)


class StableDiffusionXLLoraLoaderMixin(LoraLoaderMixin):
    """This class overrides [`LoraLoaderMixin`] with LoRA loading/saving code that's specific to SDXL."""

    # Overrride to properly handle the loading and unloading of the additional text encoder.
    def load_lora_weights(
        self,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        adapter_name: Optional[str] = None,
        **kwargs,
    ):
        """
        Load LoRA weights specified in `pretrained_model_name_or_path_or_dict` into `self.unet` and
        `self.text_encoder`.

        All kwargs are forwarded to `self.lora_state_dict`.

        See [`~loaders.LoraLoaderMixin.lora_state_dict`] for more details on how the state dict is loaded.

        See [`~loaders.LoraLoaderMixin.load_lora_into_unet`] for more details on how the state dict is loaded into
        `self.unet`.

        See [`~loaders.LoraLoaderMixin.load_lora_into_text_encoder`] for more details on how the state dict is loaded
        into `self.text_encoder`.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                A string (model id of a pretrained model hosted on the Hub), a path to a directory containing the model
                weights, or a [torch state
                dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).
            kwargs (`dict`, *optional*):
                See [`~loaders.LoraLoaderMixin.lora_state_dict`].
            adapter_name (`str`, *optional*):
                Name for referencing the loaded adapter model. If not specified, it will use `default_{i}` where `i` is
                the total number of adapters being loaded. Must have PEFT installed to use.

        Example:

        ```py
        from diffusers import StableDiffusionXLPipeline
        import torch

        pipeline = StableDiffusionXLPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        ```
        """
        # We could have accessed the unet config from `lora_state_dict()` too. We pass
        # it here explicitly to be able to tell that it's coming from an SDXL
        # pipeline.

        # First, ensure that the checkpoint is a compatible one and can be successfully loaded.
        state_dict, network_alphas = self.lora_state_dict(
            pretrained_model_name_or_path_or_dict,
            unet_config=self.unet.config,
            **kwargs,
        )
        is_correct_format = all("lora" in key for key in state_dict.keys())
        if not is_correct_format:
            raise ValueError("Invalid LoRA checkpoint.")

        self.load_lora_into_unet(
            state_dict, network_alphas=network_alphas, unet=self.unet, adapter_name=adapter_name, _pipeline=self
        )
        text_encoder_state_dict = {k: v for k, v in state_dict.items() if "text_encoder." in k}
        if len(text_encoder_state_dict) > 0:
            self.load_lora_into_text_encoder(
                text_encoder_state_dict,
                network_alphas=network_alphas,
                text_encoder=self.text_encoder,
                prefix="text_encoder",
                lora_scale=self.lora_scale,
                adapter_name=adapter_name,
                _pipeline=self,
            )

        text_encoder_2_state_dict = {k: v for k, v in state_dict.items() if "text_encoder_2." in k}
        if len(text_encoder_2_state_dict) > 0:
            self.load_lora_into_text_encoder(
                text_encoder_2_state_dict,
                network_alphas=network_alphas,
                text_encoder=self.text_encoder_2,
                prefix="text_encoder_2",
                lora_scale=self.lora_scale,
                adapter_name=adapter_name,
                _pipeline=self,
            )

    @classmethod
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        unet_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_2_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the LoRA parameters corresponding to the UNet and text encoder.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to. Will be created if it doesn't exist.
            unet_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `unet`.
            text_encoder_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dictionary. Useful during distributed training when you need to
                replace `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way with `pickle`.
        """
        state_dict = {}

        def pack_weights(layers, prefix):
            layers_weights = layers.state_dict() if isinstance(layers, torch.nn.Module) else layers
            layers_state_dict = {f"{prefix}.{module_name}": param for module_name, param in layers_weights.items()}
            return layers_state_dict

        if not (unet_lora_layers or text_encoder_lora_layers or text_encoder_2_lora_layers):
            raise ValueError(
                "You must pass at least one of `unet_lora_layers`, `text_encoder_lora_layers` or `text_encoder_2_lora_layers`."
            )

        if unet_lora_layers:
            state_dict.update(pack_weights(unet_lora_layers, "unet"))

        if text_encoder_lora_layers and text_encoder_2_lora_layers:
            state_dict.update(pack_weights(text_encoder_lora_layers, "text_encoder"))
            state_dict.update(pack_weights(text_encoder_2_lora_layers, "text_encoder_2"))

        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )

    def _remove_text_encoder_monkey_patch(self):
        if USE_PEFT_BACKEND:
            recurse_remove_peft_layers(self.text_encoder)
            # TODO: @younesbelkada handle this in transformers side
            if getattr(self.text_encoder, "peft_config", None) is not None:
                del self.text_encoder.peft_config
                self.text_encoder._hf_peft_config_loaded = None

            recurse_remove_peft_layers(self.text_encoder_2)
            if getattr(self.text_encoder_2, "peft_config", None) is not None:
                del self.text_encoder_2.peft_config
                self.text_encoder_2._hf_peft_config_loaded = None
        else:
            self._remove_text_encoder_monkey_patch_classmethod(self.text_encoder)
            self._remove_text_encoder_monkey_patch_classmethod(self.text_encoder_2)
