from typing import List, Optional, Callable
from PIL import Image
import torch
import torch.nn.functional as F
import matplotlib.pyplot as plt
from config import RunConfig
from torchvision import transforms
from constants import OUT_INDEX, STRUCT_INDEX, STYLE1_INDEX, STYLE2_INDEX, MOD_STEP
from models.stable_diffusion import CrossImageAttentionStableDiffusionPipeline
from utils import attention_utils
from utils.adain import masked_adain, adain, masked_adain_half_mask
from utils.model_utils import get_stable_diffusion_model
from utils.segmentation import Segmentor
from utils.sam_segmentation import sam_segmentation_flow
from utils.create_attention_maps import create_maps
import numpy as np
import os
import datetime


class AppearanceTransferModel:

    def __init__(self, config: RunConfig, pipe: Optional[CrossImageAttentionStableDiffusionPipeline] = None):
        self.config = config
        self.pipe = get_stable_diffusion_model() if pipe is None else pipe
        self.register_attention_control()
        self.segmentor = Segmentor(prompt=config.prompt, object_nouns=[config.object_noun])
        self.latents_app1, self.latents_app2, self.latents_struct = None, None, None
        self.zs_app1, self.zs_app2, self.zs_struct = None, None, None
        self.image_app1_mask_32, self.image_app1_mask_64 = None, None
        self.image_app2_mask_32, self.image_app2_mask_64 = None, None
        self.image_struct_mask_32, self.image_struct_mask_64 = None, None
        self.object1_mask_32, self.object1_mask_64 = None, None
        self.object2_mask_32, self.object2_mask_64 = None, None
        self.register_attention_control()
        self.enable_edit = False
        self.step = 0

    def set_latents(self, latents_app1: torch.Tensor, latents_app2: torch.Tensor, latents_struct: torch.Tensor):
        self.latents_app1 = latents_app1
        self.latents_app2 = latents_app2
        self.latents_struct = latents_struct

    def set_noise(self, zs_app1: torch.Tensor, zs_app2: torch.Tensor, zs_struct: torch.Tensor):
        self.zs_app1 = zs_app1
        self.zs_app2 = zs_app2
        self.zs_struct = zs_struct

    def save_mask(self, mask, name, step):
        if mask is not None:
            # Convert the PyTorch tensor to a NumPy array
            # Ensure the tensor is on CPU and detached from the computation graph
            mask_array = mask.cpu().detach().numpy()

            # Convert to uint8 if necessary (common for image data)
            # This step assumes your mask is already scaled appropriately (0-255)
            if mask_array.dtype != np.uint8:
                mask_array = (mask_array * 255).astype(np.uint8)

            # Convert NumPy array to image
            img = Image.fromarray(mask_array)

            # Ensure the directory exists
            import os
            save_path = f'./saved_masks/{name}_step_{step}.png'
            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            # Save the image with a descriptive filename
            img.save(save_path)
            print(f"{name} is not None, saved at step: {step}")

    def set_masks(self):
        struct_mask_dict_lst = sam_segmentation_flow(self.config.struct_image_path, n_objects=2)
        app1_mask_dict_lst = sam_segmentation_flow(self.config.app1_image_path, n_objects=1)
        app2_mask_dict_lst = sam_segmentation_flow(self.config.app2_image_path, n_objects=1)

        self.object1_mask_32 = struct_mask_dict_lst[0][(32, 32)]
        self.object1_mask_64 = struct_mask_dict_lst[0][(64, 64)]
        self.object2_mask_32 = struct_mask_dict_lst[1][(32, 32)]
        self.object2_mask_64 = struct_mask_dict_lst[1][(64, 64)]

        self.image_app1_mask_32 = app1_mask_dict_lst[0][(32, 32)]
        self.image_app1_mask_64 = app1_mask_dict_lst[0][(64, 64)]
        self.image_app2_mask_32 = app2_mask_dict_lst[0][(32, 32)]
        self.image_app2_mask_64 = app2_mask_dict_lst[0][(64, 64)]

        #self.visualize_masks()  # Visualize masks when they are set, new function

        # Call save_segmented_objects to save masks right after they are set
        #segmented_masks = [self.image_app1_mask_32, self.image_app2_mask_32, self.image_struct_mask_32,
        #                   self.image_app1_mask_64, self.image_app2_mask_64, self.image_struct_mask_64]
        #self.save_segmented_objects(segmented_masks, "./segmentation_outputs")


    def save_segmented_objects(self, segmented_masks: List[torch.Tensor], save_path: str):
        # Ensure the directory exists
        import os
        os.makedirs(save_path, exist_ok=True)

        for i, mask in enumerate(segmented_masks):
            # Convert mask to PIL image
            mask_image = Image.fromarray(mask.cpu().numpy().astype("uint8") * 255)
            mask_image.save(f"{save_path}/segment_{i}.png")

    def visualize_masks(self):
        # This method visualizes masks for debugging or inspection purposes
        fig, ax = plt.subplots(2, 3, figsize=(10, 8))
        ax[0, 0].imshow(self.image_app1_mask_32.cpu().numpy(), cmap='gray')
        ax[0, 0].set_title('Appearance Mask 32x32')
        ax[0, 1].imshow(self.image_struct_mask_32.cpu().numpy(), cmap='gray')
        ax[0, 1].set_title('Structure Mask 32x32')
        ax[0, 2].imshow(self.image_app2_mask_32.cpu().numpy(), cmap='gray')
        ax[0, 2].set_title('Appearance Mask 2, 32x32')
        ax[1, 0].imshow(self.image_app1_mask_64.cpu().numpy(), cmap='gray')
        ax[1, 0].set_title('Appearance Mask 64x64')
        ax[1, 1].imshow(self.image_struct_mask_64.cpu().numpy(), cmap='gray')
        ax[1, 1].set_title('Structure Mask 64x64')
        ax[1, 2].imshow(self.image_app2_mask_64.cpu().numpy(), cmap='gray')
        ax[1, 2].set_title('Appearance Mask 2, 64x64')
        plt.tight_layout()
        # plt.show()

    def get_adain_callback(self):

        def callback(st: int, timestep: int, latents: torch.FloatTensor) -> Callable:
            self.step = st
            # Compute the masks using prompt mixing self-segmentation and use the masks for AdaIN operation
            if self.config.use_masked_adain and self.step == self.config.adain_range.start:
                masks = self.segmentor.get_object_masks(is_cross=True, step=self.step)
                #self.set_masks(masks)
                print("set masks at step: ", self.step)
            else:
                if self.object1_mask_32 is None:
                    self.set_masks()
                    print()

            # Apply AdaIN operation using the computed masks
            if self.config.adain_range.start <= self.step < self.config.adain_range.end:
                if self.config.use_masked_adain:
                    latents[OUT_INDEX] = masked_adain(latents[OUT_INDEX], latents[STYLE1_INDEX], latents[STYLE2_INDEX],
                                                      self.image_struct_mask_64, self.image_app1_mask_64,
                                                      self.image_app2_mask_64)
                else:
                    latents[OUT_INDEX] = adain(latents[OUT_INDEX], latents[STYLE1_INDEX], latents[STYLE2_INDEX])

        return callback

    def register_attention_control(self):

        model_self = self

        class AttentionProcessor:

            def __init__(self, place_in_unet: str):
                self.place_in_unet = place_in_unet
                if not hasattr(F, "scaled_dot_product_attention"):
                    raise ImportError("AttnProcessor2_0 requires torch 2.0, to use it, please upgrade torch to 2.0.")

            def __call__(self,
                         attn,
                         hidden_states: torch.Tensor,
                         encoder_hidden_states: Optional[torch.Tensor] = None,
                         attention_mask=None,
                         temb=None,
                         perform_swap: bool = False):

                residual = hidden_states

                if attn.spatial_norm is not None:
                    hidden_states = attn.spatial_norm(hidden_states, temb)

                input_ndim = hidden_states.ndim

                if input_ndim == 4:
                    batch_size, channel, height, width = hidden_states.shape
                    hidden_states = hidden_states.view(batch_size, channel, height * width).transpose(1, 2)

                batch_size, sequence_length, _ = (
                    hidden_states.shape if encoder_hidden_states is None else encoder_hidden_states.shape
                )

                if attention_mask is not None:
                    attention_mask = attn.prepare_attention_mask(attention_mask, sequence_length, batch_size)
                    attention_mask = attention_mask.view(batch_size, attn.heads, -1, attention_mask.shape[-1])

                if attn.group_norm is not None:
                    hidden_states = attn.group_norm(hidden_states.transpose(1, 2)).transpose(1, 2)

                query = attn.to_q(hidden_states)
                is_cross = encoder_hidden_states is not None
                if not is_cross:
                    encoder_hidden_states = hidden_states
                elif attn.norm_cross:
                    encoder_hidden_states = attn.norm_encoder_hidden_states(encoder_hidden_states)


                key = attn.to_k(encoder_hidden_states)
                value = attn.to_v(encoder_hidden_states)

                inner_dim = key.shape[-1]
                head_dim = inner_dim // attn.heads
                should_mix = False
                split_attn = False
                # Potentially apply our cross image attention operation
                # To do so, we need to be in a self-attention layer in the decoder part of the denoising network
                if perform_swap and not is_cross and "up" in self.place_in_unet and model_self.enable_edit:
                    if attention_utils.should_mix_keys_and_values(model_self, hidden_states):
                        should_mix = True
                        if model_self.step % 5 == 0 and model_self.step < 40:
                            # Inject the structure's keys and values
                            key[OUT_INDEX] = key[STRUCT_INDEX]
                            value[OUT_INDEX] = value[STRUCT_INDEX]
                        else:
                            # Inject the appearance's keys and values
                            split_attn = True

                            #key, value = masked_cross_attn_keys(query, key, value, is_cross)
                            # Inject the appearance's keys and values
                            #key[OUT_INDEX] = key[STYLE2_INDEX]
                            #value[OUT_INDEX] = value[STYLE2_INDEX]
                #           # value[OUT_INDEX] = value[STYLE1_INDEX]

                query = query.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
                key = key.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
                value = value.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

                # Compute the cross attention and apply our contrasting operation
                edit_map = perform_swap and model_self.enable_edit and should_mix
                hidden_states, attn_weight = attention_utils.compute_attention(query, key, value, is_cross,
                                                                               split_attn, edit_map, model_self)

                # Update attention map for segmentation
                model_self.segmentor.update_attention(attn_weight, is_cross)
                # else:
                #     model_self.segmentor.update_attention(attn_weight, is_cross)
                hidden_states = hidden_states.transpose(1, 2).reshape(batch_size, -1, attn.heads * head_dim)
                hidden_states = hidden_states.to(query[OUT_INDEX].dtype)

                # linear proj
                hidden_states = attn.to_out[0](hidden_states)
                # dropout
                hidden_states = attn.to_out[1](hidden_states)

                if input_ndim == 4:
                    hidden_states = hidden_states.transpose(-1, -2).reshape(batch_size, channel, height, width)

                if attn.residual_connection:
                    hidden_states = hidden_states + residual

                hidden_states = hidden_states / attn.rescale_output_factor

                return hidden_states

        def register_recr(net_, count, place_in_unet):
            if net_.__class__.__name__ == 'ResnetBlock2D':
                pass
            if net_.__class__.__name__ == 'Attention':
                net_.set_processor(AttentionProcessor(place_in_unet + f"_{count + 1}"))
                return count + 1
            elif hasattr(net_, 'children'):
                for net__ in net_.children():
                    count = register_recr(net__, count, place_in_unet)
            return count

        cross_att_count = 0
        sub_nets = self.pipe.unet.named_children()
        for net in sub_nets:
            if "down" in net[0]:
                cross_att_count += register_recr(net[1], 0, "down")
            elif "up" in net[0]:
                cross_att_count += register_recr(net[1], 0, "up")
            elif "mid" in net[0]:
                cross_att_count += register_recr(net[1], 0, "mid")
        def masked_cross_attn_keys(query, key, value):

            #key[OUT_INDEX] = key[OUT_INDEX] + torch.Tensor([float('-inf')]) * binary_mask_appearance2 # masking style2
            #key[OUT_INDEX] = key[OUT_INDEX] + torch.Tensor([float('-inf')]) * binary_mask_appearance1 # masking style1

           # hidden_states, attn_weight = attention_utils.compute_scaled_dot_product_attention(
           #     query, key, value, is_cross=is_cross)

            convert_tensor = transforms.ToTensor()
            mask_style1_32 = convert_tensor(np.load("masks/cross_attention_style1_mask_resolution_32.npy"))
            mask_style2_32 = convert_tensor(np.load("masks/cross_attention_style2_mask_resolution_32.npy"))
            mask_struct_32 = convert_tensor(np.load("masks/cross_attention_structural_mask_resolution_32.npy"))
            mask_style1_64 = convert_tensor(np.load("masks/cross_attention_style1_mask_resolution_64.npy"))
            mask_style2_64 = convert_tensor(np.load("masks/cross_attention_style2_mask_resolution_64.npy"))
            mask_struct_64 = convert_tensor(np.load("masks/cross_attention_structural_mask_resolution_64.npy"))

            if query.shape[1] == 32**2:
                binary_mask_struct, binary_mask_appearance1, binary_mask_appearance2 = (mask_style1_32, mask_style2_32,
                                                                                        mask_struct_32)
            elif query.shape[1] == 64**2:
                binary_mask_struct, binary_mask_appearance1, binary_mask_appearance2 = (mask_style1_64, mask_style2_64,
                                                                                        mask_struct_64)
            else:
                return key, value


            binary_mask_appearance1 = binary_mask_appearance1.squeeze().flatten()
            binary_mask_appearance2 = binary_mask_appearance2.squeeze().flatten()
            inv_binary_mask_appearance1 = np.bitwise_not(binary_mask_appearance1) + 2
            inv_binary_mask_appearance2 = np.bitwise_not(binary_mask_appearance2) + 2
            binary_mask_appearance1 = np.reshape(binary_mask_appearance1,(binary_mask_appearance1.shape[0],1)).to('cuda')
            binary_mask_appearance2 = np.reshape(binary_mask_appearance2,(binary_mask_appearance2.shape[0],1)).to('cuda')
            inv_binary_mask_appearance1 = np.reshape(inv_binary_mask_appearance1,(inv_binary_mask_appearance1.shape[0],1)).to('cuda')
            inv_binary_mask_appearance2 = np.reshape(inv_binary_mask_appearance2,(inv_binary_mask_appearance2.shape[0],1)).to('cuda')

            # Using k,v from style 1 on object 1
            key[OUT_INDEX] = key[OUT_INDEX] * inv_binary_mask_appearance1 + key[STYLE1_INDEX] * binary_mask_appearance1 # adding k of style1
            value[OUT_INDEX] = value[OUT_INDEX] * inv_binary_mask_appearance1 + value[STYLE1_INDEX] * binary_mask_appearance1 # adding v of style1
            # Using k,v from style 2 on object 2
            key[OUT_INDEX] = key[OUT_INDEX] * inv_binary_mask_appearance2 + key[STYLE2_INDEX] * binary_mask_appearance2 # adding k of style2
            value[OUT_INDEX] = value[OUT_INDEX] * inv_binary_mask_appearance2 + value[STYLE2_INDEX] * binary_mask_appearance2 # adding v of style2

            return key, value
