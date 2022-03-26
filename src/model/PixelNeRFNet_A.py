import torch
import os
import warnings
from .models import PixelNeRFNet
from .model_util import make_encoder, make_mlp
from .encoder import ImageEncoder
from .code import PositionalEncoding
from contrib.model.AppearanceEncoder import AppearanceEncoder
import torch.autograd.profiler as profiler
from util import repeat_interleave
from torch import nn

class PixelNeRFNet_A(PixelNeRFNet):
    # For now, identical to the PixelNeRF
    def __init__(self, conf, stop_encoder_grad=False, stop_app_encoder_grad=False, stop_f1_grad=False, app_enc_off=False):
        """
        :param conf PyHocon config subtree 'model'
        """
        super().__init__()
        self.encoder = make_encoder(conf["encoder"])
        self.use_encoder = conf.get_bool("use_encoder", True)  # Image features?

        self.use_xyz = conf.get_bool("use_xyz", False)

        assert self.use_encoder or self.use_xyz  # Must use some feature..

        # Whether to shift z to align in canonical frame.
        # So that all objects, regardless of camera distance to center, will
        # be centered at z=0.
        # Only makes sense in ShapeNet-type setting.
        self.normalize_z = conf.get_bool("normalize_z", True)

        self.stop_encoder_grad = (
            stop_encoder_grad  # Stop ConvNet gradient (freeze weights)
        )
        self.use_code = conf.get_bool("use_code", False)  # Positional encoding
        self.use_code_viewdirs = conf.get_bool(
            "use_code_viewdirs", True
        )  # Positional encoding applies to viewdirs

        # Enable view directions
        self.use_viewdirs = conf.get_bool("use_viewdirs", False)

        # Global image features?
        self.use_global_encoder = conf.get_bool("use_global_encoder", False)

        d_latent = self.encoder.latent_size if self.use_encoder else 0
        d_in = 3 if self.use_xyz else 1

        if self.use_viewdirs and self.use_code_viewdirs:
            # Apply positional encoding to viewdirs
            d_in += 3
        if self.use_code and d_in > 0:
            # Positional encoding for x,y,z OR view z
            self.code = PositionalEncoding.from_conf(conf["code"], d_in=d_in)
            d_in = self.code.d_out
        if self.use_viewdirs and not self.use_code_viewdirs:
            # Don't apply positional encoding to viewdirs (concat after encoded)
            d_in += 3

        if self.use_global_encoder:
            # Global image feature
            self.global_encoder = ImageEncoder.from_conf(conf["global_encoder"])
            self.global_latent_size = self.global_encoder.latent_size
            d_latent += self.global_latent_size

        d_out = 4

        self.latent_size = self.encoder.latent_size
        self.mlp_coarse = make_mlp(
            conf["mlp_coarse"], 
            d_in, d_latent, 
            d_out=d_out, 
            stop_f1_grad=stop_f1_grad,
            app_enc_off=app_enc_off
        )
        self.mlp_fine = make_mlp(
            conf["mlp_fine"], 
            d_in, 
            d_latent, 
            d_out=d_out, 
            allow_empty=True, 
            stop_f1_grad=stop_f1_grad,
            app_enc_off=app_enc_off
        )
        # Note: this is world -> camera, and bottom row is omitted
        self.register_buffer("poses", torch.empty(1, 3, 4), persistent=False)
        self.register_buffer("image_shape", torch.empty(2), persistent=False)

        self.d_in = d_in
        self.d_out = d_out
        self.d_latent = d_latent
        self.register_buffer("focal", torch.empty(1, 2), persistent=False)
        # Principal point
        self.register_buffer("c", torch.empty(1, 2), persistent=False)

        self.num_objs = 0
        self.num_views_per_obj = 1

        # Appearance encoder additions
        self.stop_app_encoder_grad = stop_app_encoder_grad
        self.app_encoder = AppearanceEncoder(conf["app_encoder"])


    
    def load_weights(self, args, opt_init=False, strict=False, device=None):
        """
        Helper for loading weights according to argparse arguments.
        Your can put a checkpoint at checkpoints/<exp>/pixel_nerf_init to use as initialization.
        :param opt_init if true, loads from init checkpoint instead of usual even when resuming
        """
        self = super().load_weights(args, opt_init, strict, device)

        # Only load weights for our appearance encoder if we want to
        if args.load_app_encoder:
            model_path = "%s/%s/%s" % (args.checkpoints_path, args.name, "app_encoder_init")

            if device is None:
                device = self.poses.device

            if os.path.exists(model_path):
                print("Load (Appearance Encoder)", model_path)
                self.app_encoder.load_state_dict(
                    torch.load(model_path, map_location=device), strict=True
                )
            else:
                warnings.warn(
                    (
                        "WARNING: {} does not exist, not loaded!! Apearance encoder will be re-initialized.\n"
                        + "If you are trying to load a pretrained appearance encoder for PixelNeRF-A, STOP since it's not in the right place. "
                    ).format(model_path)
                )
        # Otherwise, initialize the weights for the encoder using Kaming Normal initialization
        else:
            def init_weights(m):
                if isinstance(m, (nn.Linear, nn.Conv2d)):
                    nn.init.kaiming_normal_(m.weight)
                    if m.bias.data is not None:
                        m.bias.data.zero_()
                elif isinstance(m, (nn.BatchNorm2d, nn.BatchNorm1d)):
                    m.weight.data.fill_(1)
                    if m.bias.data is not None:
                        m.bias.data.zero_()
                
            self.app_encoder.apply(init_weights)
        
        return self
    
    def forward(self, xyz, coarse=True, viewdirs=None, far=False):
        """
        Predict (r, g, b, sigma) at world space points xyz.
        Please call encode first!
        :param xyz (SB, B, 3)
        SB is batch of objects
        B is batch of points (in rays)
        NS is number of input views
        :return (SB, B, 4) r g b sigma
        """
        with profiler.record_function("model_inference"):
            SB, B, _ = xyz.shape
            NS = self.num_views_per_obj

            # Transform query points into the camera spaces of the input views
            xyz = repeat_interleave(xyz, NS)  # (SB*NS, B, 3)
            xyz_rot = torch.matmul(self.poses[:, None, :3, :3], xyz.unsqueeze(-1))[
                ..., 0
            ]
            xyz = xyz_rot + self.poses[:, None, :3, 3]

            if self.d_in > 0:
                # * Encode the xyz coordinates
                if self.use_xyz:
                    if self.normalize_z:
                        z_feature = xyz_rot.reshape(-1, 3)  # (SB*B, 3)
                    else:
                        z_feature = xyz.reshape(-1, 3)  # (SB*B, 3)
                else:
                    if self.normalize_z:
                        z_feature = -xyz_rot[..., 2].reshape(-1, 1)  # (SB*B, 1)
                    else:
                        z_feature = -xyz[..., 2].reshape(-1, 1)  # (SB*B, 1)

                if self.use_code and not self.use_code_viewdirs:
                    # Positional encoding (no viewdirs)
                    z_feature = self.code(z_feature)

                if self.use_viewdirs:
                    # * Encode the view directions
                    assert viewdirs is not None
                    # Viewdirs to input view space
                    viewdirs = viewdirs.reshape(SB, B, 3, 1)
                    viewdirs = repeat_interleave(viewdirs, NS)  # (SB*NS, B, 3, 1)
                    viewdirs = torch.matmul(
                        self.poses[:, None, :3, :3], viewdirs
                    )  # (SB*NS, B, 3, 1)
                    viewdirs = viewdirs.reshape(-1, 3)  # (SB*B, 3)
                    z_feature = torch.cat(
                        (z_feature, viewdirs), dim=1
                    )  # (SB*B, 4 or 6)

                if self.use_code and self.use_code_viewdirs:
                    # Positional encoding (with viewdirs)
                    z_feature = self.code(z_feature)

                mlp_input = z_feature

            if self.use_encoder:
                # Grab encoder's latent code.
                uv = -xyz[:, :, :2] / xyz[:, :, 2:]  # (SB, B, 2)
                uv *= repeat_interleave(
                    self.focal.unsqueeze(1), NS if self.focal.shape[0] > 1 else 1
                )
                uv += repeat_interleave(
                    self.c.unsqueeze(1), NS if self.c.shape[0] > 1 else 1
                )  # (SB*NS, B, 2)
                latent = self.encoder.index(
                    uv, None, self.image_shape
                )  # (SB * NS, latent, B)

                if self.stop_encoder_grad:
                    latent = latent.detach()
                latent = latent.transpose(1, 2).reshape(
                    -1, self.latent_size
                )  # (SB * NS * B, latent)

                if self.d_in == 0:
                    # z_feature not needed
                    mlp_input = latent
                else:
                    mlp_input = torch.cat((latent, z_feature), dim=-1)

            if self.use_global_encoder:
                # Concat global latent code if enabled
                global_latent = self.global_encoder.latent
                assert mlp_input.shape[0] % global_latent.shape[0] == 0
                num_repeats = mlp_input.shape[0] // global_latent.shape[0]
                global_latent = repeat_interleave(global_latent, num_repeats)
                mlp_input = torch.cat((global_latent, mlp_input), dim=-1)
            
            # Added appearance encoder as input to MLP
            app_enc = self.app_encoder.app_encoding
            if self.stop_app_encoder_grad:
                app_enc = app_enc.detach()

            # Camera frustum culling stuff, currently disabled
            combine_index = None
            dim_size = None

            # Run main NeRF network
            if coarse or self.mlp_fine is None:
                mlp_output = self.mlp_coarse(
                    mlp_input,
                    app_enc,
                    combine_inner_dims=(self.num_views_per_obj, B),
                    combine_index=combine_index,
                    dim_size=dim_size,
                )
            else:
                mlp_output = self.mlp_fine(
                    mlp_input,
                    app_enc,
                    combine_inner_dims=(self.num_views_per_obj, B),
                    combine_index=combine_index,
                    dim_size=dim_size,
                )

            # Interpret the output
            mlp_output = mlp_output.reshape(-1, B, self.d_out)

            rgb = mlp_output[..., :3]
            sigma = mlp_output[..., 3:4]

            output_list = [torch.sigmoid(rgb), torch.relu(sigma)]
            output = torch.cat(output_list, dim=-1)
            output = output.reshape(SB, B, -1)
        return output