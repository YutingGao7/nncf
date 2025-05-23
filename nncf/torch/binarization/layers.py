# Copyright (c) 2024 Intel Corporation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
from torch import nn

from nncf.common.logging import nncf_logger
from nncf.common.utils.registry import Registry
from nncf.torch.binarization.binarize_functions import ActivationBinarizationScaleThresholdFn
from nncf.torch.binarization.binarize_functions import DOREFABinarizeFn
from nncf.torch.binarization.binarize_functions import XNORBinarizeFn
from nncf.torch.dynamic_graph.patch_pytorch import register_operator
from nncf.torch.layer_utils import COMPRESSION_MODULES
from nncf.torch.layer_utils import CompressionParameter
from nncf.torch.quantization.layers import get_per_channel_scale_shape

BINARIZATION_MODULES = Registry("binarization_modules")


class BinarizationMode:
    XNOR = "xnor"
    DOREFA = "dorefa"


class BaseBinarizer(nn.Module):
    def __init__(self, enabled=False):
        super().__init__()
        self.register_buffer("enabled", torch.IntTensor([0]))
        if enabled:
            self.enable()

    def forward(self, x):
        if self.is_enabled():
            return self.binarize(x)
        return x

    def binarize(self, x):
        raise NotImplementedError

    def enable(self):
        self.enabled[0] = 1

    def disable(self):
        self.enabled[0] = 0

    def is_enabled(self):
        return self.enabled[0] == 1


class WeightBinarizer(BaseBinarizer):
    def binarize(self, x):
        raise NotImplementedError


class ActivationBinarizer(BaseBinarizer):
    def binarize(self, x):
        raise NotImplementedError


@COMPRESSION_MODULES.register()
@BINARIZATION_MODULES.register(BinarizationMode.XNOR)
class XNORBinarize(WeightBinarizer):
    def binarize(self, x):
        return xnor_binarize_op(x)


@register_operator()
def xnor_binarize_op(x):
    return XNORBinarizeFn.apply(x)


@COMPRESSION_MODULES.register()
@BINARIZATION_MODULES.register(BinarizationMode.DOREFA)
class DOREFABinarize(WeightBinarizer):
    def binarize(self, x):
        return dorefa_binarize_op(x)


@register_operator()
def dorefa_binarize_op(x):
    return DOREFABinarizeFn.apply(x)


# Activation binarization module
class ActivationBinarizationScaleThreshold(ActivationBinarizer):
    def __init__(self, input_shape, enabled=False, compression_lr_multiplier=None, desc=""):
        super().__init__(enabled)

        self.input_shape = input_shape

        self.scale = CompressionParameter(
            torch.Tensor([0]), requires_grad=enabled, compression_lr_multiplier=compression_lr_multiplier
        )
        self.scale.data.zero_()

        # Need scale_initialized as buffer for it to appear in the model state dict
        self.register_buffer("scale_initialized", torch.IntTensor([0]))

        threshold_shape = get_per_channel_scale_shape(self.input_shape, is_weights=False)
        self.threshold = CompressionParameter(
            torch.ones(threshold_shape), requires_grad=enabled, compression_lr_multiplier=compression_lr_multiplier
        )
        self.threshold.data.zero_()
        self.bin = activation_bin_scale_threshold_op

    @property
    def is_scale_initialized(self):
        return self.scale_initialized[0] == 1

    @is_scale_initialized.setter
    def is_scale_initialized(self, value: bool):
        self.scale_initialized[0] = 1 if value else 0

    def binarize(self, x):
        if self.training and not self.is_scale_initialized:
            # init scale using nonbinarized activation statistics
            d = x.detach().data.contiguous().view(-1)
            top_num = max(1, round(d.shape[0] * 0.001))
            topk_res = d.topk(top_num)
            scale = topk_res[0].min()
            nncf_logger.debug(f"Binarized activation scale set to: {scale.item()}")
            self.scale.data[:] = scale.log()
            self.is_scale_initialized = True

        x = self.bin(x, self.scale.exp(), self.threshold.sigmoid())

        return x

    def enable(self):
        super().enable()
        self.scale.requires_grad_(True)
        self.threshold.requires_grad_(True)


@register_operator()
def activation_bin_scale_threshold_op(x, scale, threshold):
    return ActivationBinarizationScaleThresholdFn.apply(x, scale, threshold)
