# instead of sharing the middle weights for the kernal, we take the softplus/abs of the middle weights to make sure
# the product is none negative

import torch
import torch.utils.data
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import numpy as np
import math
from .utils import *


def leaky_relu_derivative(x, slope):
    slope1 = torch.ones_like(x).detach()
    slope2 = torch.ones_like(x).detach() * slope
    return torch.where(x > 0, slope1, slope2).detach()


def elu_derivative(x, slope=1.0):
    slope1 = torch.ones_like(x).detach()
    slope2 = torch.exp(x).detach() * slope
    return torch.where(x > 0, slope1, slope2).detach()


def leaky_relu(x, log_det, slope):
    slope1 = torch.ones_like(x).detach()
    slope2 = torch.ones_like(x).detach() * slope
    x = F.leaky_relu(x, negative_slope=slope)
    log_det += torch.sum(torch.log(torch.where(x > 0, slope1, slope2)))
    return x, log_det


class Leaky_Relu(nn.Module):
    """ An implementation of a activation normalization layer
    from Glow: Generative Flow with Invertible 1x1 Convolutions
    (https://arxiv.org/abs/1807.03039).
    """

    def __init__(self, config, slope):
        super(Leaky_Relu, self).__init__()
        self.slope = slope

    def forward(self, inputs):
        x = inputs[0]
        log_det = inputs[1]
        slope1 = torch.ones_like(x).detach()
        slope2 = torch.ones_like(x).detach() * self.slope
        x = F.leaky_relu(x, negative_slope=self.slope)
        log_det += torch.sum(torch.log(torch.where(x > 0, slope1, slope2)))
        return x, log_det


# invertible batch norm 
# see paper: Masked Autoregressive Flow for Density Estimation
EPSILON = 1e-8
'''
def batch_norm(output, log_det):
    m = output.mean().item()
    v = output.var().item()
    u = (output - m) /np.sqrt(v + EPSILON)
    log_det += np.sum(- 0.5 * np.log(v + EPSILON))
    return u, log_det
'''


def batch_norm(output, log_det, beta, gamma):
    m = output.mean().item()
    v = output.var().item()
    u = (output - m) * torch.exp(gamma) / np.sqrt(v + EPSILON) + beta
    log_det += torch.sum(gamma - 0.5 * np.log(v + EPSILON))
    return u, log_det


class ActNorm(nn.Module):
    """ An implementation of a activation normalization layer
    from Glow: Generative Flow with Invertible 1x1 Convolutions
    (https://arxiv.org/abs/1807.03039).
    """

    def __init__(self, config, num_inputs):
        super(ActNorm, self).__init__()
        self.weight = nn.Parameter(torch.ones(num_inputs)).to(config.device)
        self.bias = nn.Parameter(torch.zeros(num_inputs)).to(config.device)
        self.initialized = False

    def forward(self, inputs, log_det):
        if self.initialized == False:
            self.weight.data.copy_(1 / (inputs.std(0) + 1e-12))
            self.bias.data.copy_(inputs.mean(0))
            self.initialized = True
        return (inputs - self.bias) * self.weight, log_det + inputs.size(0) * torch.sum(
            torch.log(torch.abs(self.weight)))



# DO NOT FORGET ACTNORM!!!
class FundamentalBlock(nn.Module):
    # Input_dim should be 1(grey scale image) or 3(RGB image), or other dimension if use SpaceToDepth

    def init_conv_weight(self, weight):
        init.kaiming_uniform_(weight, a=math.sqrt(5))
        # init.xavier_normal_(weight)

    def init_conv_bias(self, weight, bias):
        fan_in, _ = init._calculate_fan_in_and_fan_out(weight)
        bound = 1 / math.sqrt(fan_in)
        init.uniform_(bias, -bound, bound)

    def __init__(self, config, latent_dim, type, input_dim=3, kernel=3, padding=1, stride=1):
        super().__init__()
        self.input_dim = input_dim
        self.latent_dim = latent_dim
        self.kernel = kernel
        self.stride = stride
        self.padding = padding
        self.res = nn.Parameter(torch.ones(1))

        self.weight1 = nn.Parameter(
            torch.zeros(input_dim * latent_dim, input_dim, kernel, kernel, device=config.device)
        )
        self.bias1 = nn.Parameter(
            torch.zeros(input_dim * latent_dim, device=config.device)
        )
        self.center1 = nn.Parameter(
            torch.randn(input_dim * latent_dim, input_dim, kernel, kernel, device=config.device)
        )
        self.init_conv_weight(self.weight1)
        self.init_conv_bias(self.weight1, self.bias1)
        self.init_conv_weight(self.center1)

        self.weight2 = nn.Parameter(
            torch.zeros(input_dim * latent_dim, input_dim, kernel, kernel, device=config.device)
        )
        self.bias2 = nn.Parameter(
            torch.zeros(input_dim * latent_dim, device=config.device)
        )
        self.center2 = nn.Parameter(
            torch.randn(input_dim * latent_dim, input_dim, kernel, kernel, device=config.device)
        )

        self.init_conv_weight(self.weight2)
        self.init_conv_bias(self.weight2, self.bias2)
        self.init_conv_weight(self.center2)

        # Define masks
        kernel_mid_y, kernel_mid_x = kernel // 2, kernel // 2
        # zero in the middle(technically not middle, depending on channels), one elsewhere
        # used to mask out the diagonal element
        self.mask0 = np.ones((input_dim, input_dim, kernel, kernel), dtype=np.float32)

        # 1 in the middle, zero elsewhere, used for center mask to zero out the non-diagonal element
        self.mask1 = np.zeros((input_dim, input_dim, kernel, kernel), dtype=np.float32)

        # Mask out the element above diagonal
        self.mask = np.ones((input_dim, input_dim, kernel, kernel), dtype=np.float32)

        # For RGB ONLY:i=0:Red channel;i=1:Green channel;i=2:Blue channel
        if type == 'A':
            for i in range(input_dim):
                self.mask0[i, i, kernel_mid_y, kernel_mid_x] = 0.0
                self.mask1[i, i, kernel_mid_y, kernel_mid_x] = 1.0
                self.mask[i, :, kernel_mid_y + 1:, :] = 0.0
                # For the current and previous color channels, including the current color
                self.mask[i, :i + 1, kernel_mid_y, kernel_mid_x + 1:] = 0.0
                # For the latter color channels, not including the current color
                self.mask[i, i + 1:, kernel_mid_y, kernel_mid_x:] = 0.0
        elif type == 'B':
            for i in range(input_dim):
                self.mask0[i, i, kernel_mid_y, kernel_mid_x] = 0.0
                self.mask1[i, i, kernel_mid_y, kernel_mid_x] = 1.0
                self.mask[i, :, :kernel_mid_y, :] = 0.0
                # For the current and latter color channels, including the current color
                self.mask[i, i:, kernel_mid_y, :kernel_mid_x] = 0.0
                # For the previous color channels, not including the current color
                self.mask[i, :i, kernel_mid_y, :kernel_mid_x + 1] = 0.0
        else:
            raise TypeError('type should be either A or B')

        self.mask0 = torch.tensor(self.mask0, device=config.device)
        self.mask1 = torch.tensor(self.mask1, device=config.device)
        self.mask = torch.tensor(self.mask, device=config.device)

        self.mask0 = self.mask0.repeat(latent_dim, 1, 1, 1)
        self.mask1 = self.mask1.repeat(latent_dim, 1, 1, 1)
        self.mask = self.mask.repeat(latent_dim, 1, 1, 1)

    def forward(self, x):
        log_det = x[1]
        x = x[0]
        residual = x

        # masked_weight1 = (self.weight1 * self.mask0 + F.softplus(self.center1) * self.mask1) * self.mask
        masked_weight1 = (self.weight1 * self.mask0 + torch.abs(self.center1) * self.mask1) * self.mask
        latent_output = F.conv2d(x.repeat(1, self.latent_dim, 1, 1), masked_weight1, bias=self.bias1,
                                 padding=self.padding, stride=self.stride,
                                 groups=self.latent_dim)

        center1_diag = self.center1.view(self.latent_dim, self.input_dim, self.input_dim, self.kernel, self.kernel)

        center1_diag = torch.diagonal(center1_diag[..., self.kernel // 2, self.kernel // 2], dim1=-2, dim2=-1)

        # center1_diag = F.softplus(center1_diag)
        center1_diag = torch.abs(center1_diag)

        center2_diag = self.center2.view(self.latent_dim, self.input_dim, self.input_dim, self.kernel, self.kernel)
        center2_diag = torch.diagonal(center2_diag[..., self.kernel // 2, self.kernel // 2], dim1=-2, dim2=-1)
        # center2_diag = F.softplus(center2_diag)
        center2_diag = torch.abs(center2_diag)

        center_diag = center1_diag * center2_diag  # shape: latent_dim x input_dim
        latent_output_elu_derivative = elu_derivative(latent_output,
                                                      1)  # shape: B x latent_dim . input_dim x kernel x kernel
        latent_output_elu_derivative = latent_output_elu_derivative.view(-1, self.latent_dim, self.input_dim,
                                                                         latent_output.shape[-2],
                                                                         latent_output.shape[-1])

        self.diag = (center_diag[..., None, None] * latent_output_elu_derivative).sum(1)
        latent1 = F.elu(latent_output, alpha=1)

        masked_weight2 = (self.weight2 * self.mask0 + torch.abs(self.center2) * self.mask1) * self.mask

        output = F.conv2d(latent1, masked_weight2, self.bias2, padding=self.padding, stride=self.stride,
                          groups=self.latent_dim)
        output = output.view(output.shape[0], self.latent_dim, self.input_dim, output.shape[2], output.shape[3])
        output = output.sum(dim=1) / self.latent_dim

        mask_res = (self.res > 0).float().to(x.device)

        # MIGHT NEED TO ADD EPSILON TO self.res * mask_res
        output = output + self.res * mask_res * residual
        self.diag = self.diag / self.latent_dim + self.res * mask_res

        log_det += torch.sum(torch.log(self.diag))

        # need to act_norm

        return output, log_det


# DO NOT FORGET ACTNORM!!!
class BasicBlock(nn.Module):
    # Input_dim should be 1(grey scale image) or 3(RGB image), or other dimension if use SpaceToDepth

    def init_conv_weight(self, weight):
        init.kaiming_uniform_(weight, a=math.sqrt(5))
        # init.xavier_normal_(weight)

    def init_conv_bias(self, weight, bias):
        fan_in, _ = init._calculate_fan_in_and_fan_out(weight)
        bound = 1 / math.sqrt(fan_in)
        init.uniform_(bias, -bound, bound)

    def __init__(self, config, x_shape, latent_dim, type, input_dim=3, kernel1=3, kernel2=3, kernel3=3, padding=1,
                 stride=1, init_zero=False):
        super().__init__()
        self.input_dim = input_dim
        self.latent_dim = latent_dim
        # self.kernel = kernel
        self.stride = stride
        self.padding = padding
        self.res = nn.Parameter(torch.ones(1))

        self.weight1 = nn.Parameter(
            torch.randn(input_dim * latent_dim, input_dim, kernel1, kernel1) * 1e-5
        )
        self.bias1 = nn.Parameter(
            torch.zeros(input_dim * latent_dim)
        )

        if not init_zero:
            self.init_conv_weight(self.weight1)
            self.init_conv_bias(self.weight1, self.bias1)

        self.weight2 = nn.Parameter(
            torch.randn(input_dim * latent_dim, input_dim * latent_dim, kernel2, kernel2) * 1e-5
            #torch.zeros(input_dim * latent_dim, input_dim * latent_dim, kernel2, kernel2)
        )
        self.bias2 = nn.Parameter(
            torch.zeros(input_dim * latent_dim)
        )

        if not init_zero:
            self.init_conv_weight(self.weight2)
            self.init_conv_bias(self.weight2, self.bias2)

        self.weight3 = nn.Parameter(
            #torch.zeros(input_dim, input_dim * latent_dim, kernel3, kernel3)
            torch.randn(input_dim, input_dim * latent_dim, kernel3, kernel3) * 1e-5
        )

        self.bias3 = nn.Parameter(
            torch.zeros(input_dim)
        )

        if not init_zero:
            self.init_conv_weight(self.weight3)
            self.init_conv_bias(self.weight3, self.bias3)

        # Define masks

        # Mask out the element above diagonal
        self.mask1 = nn.Parameter(torch.ones_like(self.weight1), requires_grad=False)
        self.center_mask1 = nn.Parameter(torch.zeros_like(self.weight1), requires_grad=False)
        self.mask2 = nn.Parameter(torch.ones_like(self.weight2), requires_grad=False)
        self.center_mask2 = nn.Parameter(torch.zeros_like(self.weight2), requires_grad=False)
        self.mask3 = nn.Parameter(torch.ones_like(self.weight3), requires_grad=False)
        self.center_mask3 = nn.Parameter(torch.zeros_like(self.weight3), requires_grad=False)

        for i in range(latent_dim):
            fill_mask(self.mask1[i * input_dim: (i + 1) * input_dim, ...], type=type)
            fill_center_mask(self.center_mask1[i * input_dim: (i + 1) * input_dim, ...])
            fill_mask(self.mask3[:, i * input_dim: (i + 1) * input_dim, ...], type=type)
            fill_center_mask(self.center_mask3[:, i * input_dim: (i + 1) * input_dim, ...])
            for j in range(latent_dim):
                fill_mask(self.mask2[i * input_dim: (i + 1) * input_dim, j * input_dim: (j + 1) * input_dim, ...],
                          type=type)
                fill_center_mask(
                    self.center_mask2[i * input_dim: (i + 1) * input_dim, j * input_dim: (j + 1) * input_dim, ...])

        self.non_linearity = F.elu
        self.non_linearity_derivative = elu_derivative

        #self.t = nn.Parameter(torch.ones(x_shape))
        self.t = nn.Parameter(torch.tensor(1.))

    def forward(self, x):
        log_det = x[1]
        x = x[0]

        masked_weight1 = (self.weight1 * (1. - self.center_mask1) + torch.abs(
            self.weight1) * self.center_mask1) * self.mask1

        # shape: B x latent_output . input_dim x img_size x img_size
        latent_output = F.conv2d(x, masked_weight1, bias=self.bias1, padding=self.padding, stride=self.stride)

        kernel_mid_y, kernel_mid_x = masked_weight1.shape[-2] // 2, masked_weight1.shape[-1] // 2
        diag1 = torch.diagonal(
            masked_weight1[..., kernel_mid_y, kernel_mid_x].view(self.latent_dim, self.input_dim, self.input_dim),
            dim1=-2, dim2=-1)  # shape: latent_dim x input_dim

        # diag1 = torch.ones_like(latent_output. \
        #            view(x.shape[0], self.latent_dim, self.input_dim, x.shape[-2], x.shape[-1])).detach()\
        #        * diag1[None, :, :, None, None]  # shape: B x latent_dim x input_dim x img_shape x img_shape

        diag1 = self.non_linearity_derivative(latent_output). \
                    view(x.shape[0], self.latent_dim, self.input_dim, x.shape[-2], x.shape[-1]) \
                * diag1[None, :, :, None, None]  # shape: B x latent_dim x input_dim x img_shape x img_shape

        latent_output = self.non_linearity(latent_output)

        masked_weight2 = (self.weight2 * (1. - self.center_mask2) + torch.abs(
            self.weight2) * self.center_mask2) * self.mask2
        latent_output = F.conv2d(latent_output, masked_weight2, bias=self.bias2, padding=self.padding,
                                 stride=self.stride)

        kernel_mid_y, kernel_mid_x = masked_weight2.shape[-2] // 2, masked_weight2.shape[-1] // 2
        diag2 = masked_weight2[..., kernel_mid_y, kernel_mid_x].view(self.latent_dim, self.input_dim, self.latent_dim,
                                                                     self.input_dim)
        diag2 = torch.diagonal(diag2.permute(0, 2, 1, 3), dim1=-2,
                               dim2=-1)  # shape: latent_dim x latent_dim x input_dim
        diag2 = diag2[None, :, :, :, None, None]  # shape: 1 x latent_dim x latent_dim x input_dim x 1 x 1

        diag2 = torch.sum(diag2 * diag1.unsqueeze(1),
                          dim=2)  # shape: B x latent_dim x input_dim x img_shape x img_shape

        latent_output_derivative = self.non_linearity_derivative(latent_output)
        latent_output = self.non_linearity(latent_output)

        masked_weight3 = (self.weight3 * (1. - self.center_mask3) + torch.abs(
            self.weight3) * self.center_mask3) * self.mask3
        latent_output = F.conv2d(latent_output, masked_weight3, bias=self.bias3, padding=self.padding,
                                 stride=self.stride)

        kernel_mid_y, kernel_mid_x = masked_weight3.shape[-2] // 2, masked_weight3.shape[-1] // 2
        diag3 = masked_weight3[..., kernel_mid_y, kernel_mid_x].view(self.input_dim, self.latent_dim, self.input_dim)
        diag3 = torch.diagonal(diag3.permute(1, 0, 2), dim1=-2, dim2=-1)  # shape: latent_dim x input_dim
        diag3 = latent_output_derivative.view(x.shape[0], self.latent_dim, self.input_dim, x.shape[-2], x.shape[-1]) \
                * diag3[None, :, :, None, None]  # shape: B x latent_dim x input_dim x img_shape x img_shape

        diag = torch.sum(diag2 * diag3, dim=1)  # shape: B x input_dim x img_shape x img_shape

        # t = torch.max(torch.abs(self.t), torch.tensor(1e-8, device=x.device))
        t = torch.max(torch.abs(self.t), torch.tensor(1e-8, device=x.device))

        log_det += torch.sum(torch.log(diag + t))

        output = latent_output + t * x

        return output, log_det


class SpaceToDepth(nn.Module):
    def __init__(self, block_size):
        super(SpaceToDepth, self).__init__()
        self.block_size = block_size
        self.block_size_sq = block_size * block_size

    def forward(self, x):
        input = x[0]
        output = input.permute(0, 2, 3, 1)
        (batch_size, s_height, s_width, s_depth) = output.size()
        d_depth = s_depth * self.block_size_sq
        d_width = int(s_width / self.block_size)
        d_height = int(s_height / self.block_size)
        t_1 = output.split(self.block_size, 2)
        stack = [t_t.reshape(batch_size, d_height, d_depth) for t_t in t_1]
        output = torch.stack(stack, 1)
        output = output.permute(0, 2, 1, 3)
        output = output.permute(0, 3, 1, 2)
        return output, x[1]


class DepthToSpace(nn.Module):
    def __init__(self, block_size):
        super(DepthToSpace, self).__init__()
        self.block_size = block_size
        self.block_size_sq = block_size * block_size

    def forward(self, x):
        input = x[0]
        output = input.permute(0, 2, 3, 1)
        (batch_size, d_height, d_width, d_depth) = output.size()
        s_depth = int(d_depth / self.block_size_sq)
        s_width = int(d_width * self.block_size)
        s_height = int(d_height * self.block_size)
        t_1 = output.reshape(batch_size, d_height, d_width, self.block_size_sq, s_depth)
        spl = t_1.split(self.block_size, 3)
        stack = [t_t.reshape(batch_size, d_height, s_width, s_depth) for t_t in spl]
        output = torch.stack(stack, 0).transpose(0, 1).permute(0, 2, 1, 3, 4).reshape(batch_size, s_height, s_width,
                                                                                      s_depth)
        output = output.permute(0, 3, 1, 2)
        return output, x[1]


class Net(nn.Module):
    # layers latent_dim at each layer
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.inplanes = channel = config.data.channels

        image_size = config.data.image_size

        layer_size = config.model.layer_size
        latent_size = config.model.latent_size
        # fundamental_size = config.model.fundamental_size
        fundamental_size = 0
        self.space2depth = SpaceToDepth(4)
        self.depth2space = DepthToSpace(4)
        init_zero = False
        init_zero_bound = 30

        self.layers = nn.ModuleList()
        cur_layer = 0
        for layer_num, (ly, lt) in enumerate(zip(layer_size, latent_size)):
            '''
            if layer_num % 2 == 0:
                self.layers.append(SpaceToDepth(4))
                channel *= 4 * 4
            else:
                self.layers.append(SpaceToDepth(7))
                channel *= 7 * 7

            if layer_num % 2 == 0:
                self.layers.append(DepthToSpace(4))
                channel = int(channel/(4 * 4))
            else:
                self.layers.append(DepthToSpace(7))
                channel = int(channel/(7 * 7))
            '''

            if layer_num == 0:
                self.layers.append(SpaceToDepth(2))
                channel *= 2 * 2
                image_size = int(image_size / 2)

            # if layer_num == 1:
            #    self.layers.append(SpaceToDepth(2))
            #    channel *= 2 * 2
            #    image_size = int(image_size / 2)

            if cur_layer > init_zero_bound:
                init_zero = True

            x_shape = (config.training.batch_size, channel, image_size, image_size)
            self.layers.append(self._make_layer(x_shape, ly, lt, channel, init_zero))
            cur_layer += ly

            print("cur_layer: ", cur_layer)

    def _make_layer(self, x_shape, block_num, latent_dim, input_dim, init_zero, stride=1):
        layers = []
        for i in range(0, block_num):
            layers.append(BasicBlock(self.config, x_shape, latent_dim, type='A', input_dim=input_dim,
                                     init_zero=init_zero))
            # layers.append(FundamentalBlock(self.config, fundamental_dim, type='B', input_dim=input_dim))
            layers.append(BasicBlock(self.config, x_shape, latent_dim, type='B', input_dim=input_dim,
                                     init_zero=init_zero))
            # layers.append(FundamentalBlock(self.config, fundamental_dim, type='A', input_dim=input_dim))
        return nn.Sequential(*layers)

    def forward(self, x):
        log_det = torch.zeros([1], device=x.device)
        for layer_num, layer in enumerate(self.layers):
            x, log_det = layer([x, log_det])
        # x, log_det = self.depth2space([x, log_det])

        x = x.reshape(x.shape[0], -1)
        return x, log_det
