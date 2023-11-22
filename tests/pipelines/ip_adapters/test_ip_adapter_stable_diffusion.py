# coding=utf-8
# Copyright 2023 HuggingFace Inc.
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

import gc
import unittest

import numpy as np
import torch
from transformers import (
    CLIPImageProcessor,
    CLIPVisionModelWithProjection,
)

from diffusers import (
    StableDiffusionImg2ImgPipeline,
    StableDiffusionInpaintPipeline,
    StableDiffusionPipeline,
    StableDiffusionXLImg2ImgPipeline,
    StableDiffusionXLInpaintPipeline,
    StableDiffusionXLPipeline,
)
from diffusers.utils import load_image
from diffusers.utils.testing_utils import (
    enable_full_determinism,
    require_torch_gpu,
    slow,
    torch_device,
)


enable_full_determinism()


class IPAdapterNightlyTestsMixin(unittest.TestCase):
    dtype = torch.float16

    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def get_image_encoder(self, repo_id, subfolder):
        image_encoder = CLIPVisionModelWithProjection.from_pretrained(
            repo_id, subfolder=subfolder, torch_dtype=self.dtype
        ).to(torch_device)
        return image_encoder

    def get_image_processor(self, repo_id):
        image_processor = CLIPImageProcessor.from_pretrained(repo_id)
        return image_processor

    def get_dummy_inputs(self, for_image_to_image=False, for_inpainting=False, for_sdxl=False):
        image = load_image(
            "https://user-images.githubusercontent.com/24734142/266492875-2d50d223-8475-44f0-a7c6-08b51cb53572.png"
        )
        if for_sdxl:
            image = image.resize((1024, 1024))

        input_kwargs = {
            "prompt": "best quality, high quality",
            "negative_prompt": "monochrome, lowres, bad anatomy, worst quality, low quality",
            "num_inference_steps": 5,
            "generator": torch.Generator(device="cpu").manual_seed(33),
            "ip_adapter_image": image,
            "output_type": "np",
        }
        if for_image_to_image:
            image = load_image("https://huggingface.co/datasets/YiYiXu/testing-images/resolve/main/vermeer.jpg")
            ip_image = load_image("https://huggingface.co/datasets/YiYiXu/testing-images/resolve/main/river.png")

            if for_sdxl:
                image = image.resize((1024, 1024))
                ip_image = ip_image.resize((1024, 1024))

            input_kwargs.update({"image": image, "ip_adapter_image": ip_image})

        elif for_inpainting:
            image = load_image("https://huggingface.co/datasets/YiYiXu/testing-images/resolve/main/inpaint_image.png")
            mask = load_image("https://huggingface.co/datasets/YiYiXu/testing-images/resolve/main/mask.png")
            ip_image = load_image("https://huggingface.co/datasets/YiYiXu/testing-images/resolve/main/girl.png")

            if for_sdxl:
                image = image.resize((1024, 1024))
                mask = mask.resize((1024, 1024))
                ip_image = ip_image.resize((1024, 1024))

            input_kwargs.update({"image": image, "mask_image": mask, "ip_adapter_image": ip_image})

        return input_kwargs


@slow
@require_torch_gpu
class IPAdapterSDIntegrationTests(IPAdapterNightlyTestsMixin):
    def test_text_to_image(self):
        image_encoder = self.get_image_encoder(repo_id="h94/IP-Adapter", subfolder="models/image_encoder")
        pipeline = StableDiffusionPipeline.from_pretrained(
            "runwayml/stable-diffusion-v1-5", image_encoder=image_encoder, safety_checker=None, torch_dtype=self.dtype
        )
        pipeline.to(torch_device)
        pipeline.load_ip_adapter("h94/IP-Adapter", subfolder="models", weight_name="ip-adapter_sd15.bin")

        inputs = self.get_dummy_inputs()
        images = pipeline(**inputs).images
        image_slice = images[0, :3, :3, -1].flatten()

        expected_slice = np.array([0.8047, 0.8774, 0.9248, 0.9155, 0.9814, 1.0, 0.9678, 1.0, 1.0])

        assert np.allclose(image_slice, expected_slice, atol=1e-4, rtol=1e-4)

    def test_image_to_image(self):
        image_encoder = self.get_image_encoder(repo_id="h94/IP-Adapter", subfolder="models/image_encoder")
        pipeline = StableDiffusionImg2ImgPipeline.from_pretrained(
            "runwayml/stable-diffusion-v1-5", image_encoder=image_encoder, safety_checker=None, torch_dtype=self.dtype
        )
        pipeline.to(torch_device)
        pipeline.load_ip_adapter("h94/IP-Adapter", subfolder="models", weight_name="ip-adapter_sd15.bin")

        inputs = self.get_dummy_inputs(for_image_to_image=True)
        images = pipeline(**inputs).images
        image_slice = images[0, :3, :3, -1].flatten()

        expected_slice = np.array([0.2307, 0.2341, 0.2305, 0.24, 0.2268, 0.25, 0.2322, 0.2588, 0.2935])

        assert np.allclose(image_slice, expected_slice, atol=1e-4, rtol=1e-4)

    def test_inpainting(self):
        image_encoder = self.get_image_encoder(repo_id="h94/IP-Adapter", subfolder="models/image_encoder")
        pipeline = StableDiffusionInpaintPipeline.from_pretrained(
            "runwayml/stable-diffusion-v1-5", image_encoder=image_encoder, safety_checker=None, torch_dtype=self.dtype
        )
        pipeline.to(torch_device)
        pipeline.load_ip_adapter("h94/IP-Adapter", subfolder="models", weight_name="ip-adapter_sd15.bin")

        inputs = self.get_dummy_inputs(for_inpainting=True)
        images = pipeline(**inputs).images
        image_slice = images[0, :3, :3, -1].flatten()

        expected_slice = np.array([0.2705, 0.2395, 0.2209, 0.2312, 0.2102, 0.2104, 0.2178, 0.2065, 0.1997])

        assert np.allclose(image_slice, expected_slice, atol=1e-4, rtol=1e-4)


@slow
@require_torch_gpu
class IPAdapterSDXLIntegrationTests(IPAdapterNightlyTestsMixin):
    def test_text_to_image_sdxl(self):
        image_encoder = self.get_image_encoder(repo_id="h94/IP-Adapter", subfolder="sdxl_models/image_encoder")
        feature_extractor = self.get_image_processor("laion/CLIP-ViT-bigG-14-laion2B-39B-b160k")

        pipeline = StableDiffusionXLPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            image_encoder=image_encoder,
            feature_extractor=feature_extractor,
            torch_dtype=self.dtype,
        )
        pipeline.to(torch_device)
        pipeline.load_ip_adapter("h94/IP-Adapter", subfolder="sdxl_models", weight_name="ip-adapter_sdxl.bin")

        inputs = self.get_dummy_inputs()
        images = pipeline(**inputs).images
        image_slice = images[0, :3, :3, -1].flatten()

        expected_slice = np.array([0.0968, 0.0959, 0.0852, 0.0912, 0.0948, 0.093, 0.0893, 0.0932, 0.0923])

        assert np.allclose(image_slice, expected_slice, atol=1e-4, rtol=1e-4)

    def test_image_to_image_sdxl(self):
        image_encoder = self.get_image_encoder(repo_id="h94/IP-Adapter", subfolder="sdxl_models/image_encoder")
        feature_extractor = self.get_image_processor("laion/CLIP-ViT-bigG-14-laion2B-39B-b160k")

        pipeline = StableDiffusionXLImg2ImgPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            image_encoder=image_encoder,
            feature_extractor=feature_extractor,
            torch_dtype=self.dtype,
        )
        pipeline.to(torch_device)
        pipeline.load_ip_adapter("h94/IP-Adapter", subfolder="sdxl_models", weight_name="ip-adapter_sdxl.bin")

        inputs = self.get_dummy_inputs(for_image_to_image=True)
        images = pipeline(**inputs).images
        image_slice = images[0, :3, :3, -1].flatten()

        expected_slice = np.array([0.0653, 0.0704, 0.0725, 0.0741, 0.0702, 0.0647, 0.0782, 0.0799, 0.0752])

        assert np.allclose(image_slice, expected_slice, atol=1e-4, rtol=1e-4)

    def test_inpainting_sdxl(self):
        image_encoder = self.get_image_encoder(repo_id="h94/IP-Adapter", subfolder="sdxl_models/image_encoder")
        feature_extractor = self.get_image_processor("laion/CLIP-ViT-bigG-14-laion2B-39B-b160k")

        pipeline = StableDiffusionXLInpaintPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            image_encoder=image_encoder,
            feature_extractor=feature_extractor,
            torch_dtype=self.dtype,
        )
        pipeline.to(torch_device)
        pipeline.load_ip_adapter("h94/IP-Adapter", subfolder="sdxl_models", weight_name="ip-adapter_sdxl.bin")

        inputs = self.get_dummy_inputs(for_inpainting=True)
        images = pipeline(**inputs).images
        image_slice = images[0, :3, :3, -1].flatten()
        image_slice.tolist()

        expected_slice = np.array([0.1418, 0.1493, 0.1428, 0.146, 0.1491, 0.1501, 0.1473, 0.1501, 0.1516])

        assert np.allclose(image_slice, expected_slice, atol=1e-4, rtol=1e-4)
