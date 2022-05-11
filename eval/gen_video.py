import sys
import os

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import torch
import torch.nn.functional as F
import numpy as np
import imageio
import util
import warnings
from data import get_split_dataset
from render import NeRFRenderer
from model import make_model
from scipy.interpolate import CubicSpline
import tqdm
from data.AppearanceDataset import AppearanceDataset
from contrib.model.unet_tile_se_norm import StyleEncoder
from torch.nn import ZeroPad2d
from contrib import debug


def extra_args(parser):
    parser.add_argument(
        "--subset", "-S", type=int, default=0, help="Subset in data to use"
    )
    parser.add_argument(
        "--split",
        type=str,
        default="train",
        help="Split of data to use train | val | test",
    )
    parser.add_argument(
        "--source",
        "-P",
        type=str,
        default="64",
        help="Source view(s) in image, in increasing order. -1 to do random",
    )
    parser.add_argument(
        "--num_views",
        type=int,
        default=40,
        help="Number of video frames (rotated views)",
    )
    parser.add_argument(
        "--elevation",
        type=float,
        default=-10.0,
        help="Elevation angle (negative is above)",
    )
    parser.add_argument(
        "--scale", type=float, default=1.0, help="Video scale relative to input size"
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=0.0,
        help="Distance of camera from origin, default is average of z_far, z_near of dataset (only for non-DTU)",
    )
    parser.add_argument("--fps", type=int, default=30, help="FPS of video")

    parser.add_argument(
        "--appdir", "-DA", type=str, default=None, help="Appearance Dataset directory"
    )
    parser.add_argument(
        "--app_set_ind", "-IA", type=int, default=0, help="Index of image to be used for appearance harmonization"
    )
    parser.add_argument(
        "--app_ind", "-IM", type=int, default=0, help="Index of image to be used for appearance harmonization"
    )
    parser.add_argument(
        "--refencdir", "-DRE", type=str, default=None, help="Reference encoder directory (used for loss)"
    )
    return parser


args, conf = util.args.parse_args(extra_args)
args.resume = True

device = util.get_cuda(args.gpu_id[0])

dset, _, _ = get_split_dataset(
    args.dataset_format, args.datadir, training=False
)

data = dset[args.subset]
data_path = data["path"]
print("Data instance loaded:", data_path)

images = data["images"]  # (NV, 3, H, W)

poses = data["poses"]  # (NV, 4, 4)
focal = data["focal"]
if isinstance(focal, float):
    # Dataset implementations are not consistent about
    # returning float or scalar tensor in case of fx=fy
    focal = torch.tensor(focal, dtype=torch.float32)
focal = focal[None]

c = data.get("c")
if c is not None:
    c = c.to(device=device).unsqueeze(0)

NV, _, H, W = images.shape

if args.scale != 1.0:
    Ht = int(H * args.scale)
    Wt = int(W * args.scale)
    if abs(Ht / args.scale - H) > 1e-10 or abs(Wt / args.scale - W) > 1e-10:
        warnings.warn(
            "Inexact scaling, please check {} times ({}, {}) is integral".format(
                args.scale, H, W
            )
        )
    H, W = Ht, Wt

net = make_model(conf["model"]).to(device=device)
net.load_weights(args)

renderer = NeRFRenderer.from_conf(
    conf["renderer"], lindisp=dset.lindisp, eval_batch_size=args.ray_batch_size,
).to(device=device)

render_par = renderer.bind_parallel(net, args.gpu_id, simple_output=True).eval()

# Get the distance from camera to origin
z_near = dset.z_near
z_far = dset.z_far

# Reference encoder used across network
ref_encoder = StyleEncoder(4, 3, 32, 512, norm="BN", activ="relu", pad_type='reflect').to(device=device)
ref_encoder.load_state_dict(torch.load(args.refencdir))

print("Generating rays")

# app_size = None
# app_size_h = conf.get_int("data.app_data.img_size_h", None)
# app_size_w = conf.get_int("data.app_data.img_size_w", None)
# if (app_size_h is not None and app_size_w is not None):
#     app_size = (app_size_h, app_size_w)
dtu_format = hasattr(dset, "sub_format") and dset.sub_format == "dtu"

dset_app = AppearanceDataset(args.appdir, "train", image_size=(300, 600))
app_imgs = dset_app[args.app_set_ind][args.app_ind].unsqueeze(0).to(device=device)

if dtu_format:
    print("Using DTU camera trajectory")
    # Use hard-coded pose interpolation from IDR for DTU

    t_in = np.array([0, 2, 3, 5, 6]).astype(np.float32)
    pose_quat = torch.tensor(
        [
            [0.9698, 0.2121, 0.1203, -0.0039],
            [0.7020, 0.1578, 0.4525, 0.5268],
            [0.6766, 0.3176, 0.5179, 0.4161],
            [0.9085, 0.4020, 0.1139, -0.0025],
            [0.9698, 0.2121, 0.1203, -0.0039],
        ]
    )
    n_inter = args.num_views // 5
    args.num_views = n_inter * 5
    t_out = np.linspace(t_in[0], t_in[-1], n_inter * int(t_in[-1])).astype(np.float32)
    scales = np.array([2.0, 2.0, 2.0, 2.0, 2.0]).astype(np.float32)

    s_new = CubicSpline(t_in, scales, bc_type="periodic")
    s_new = s_new(t_out)

    q_new = CubicSpline(t_in, pose_quat.detach().cpu().numpy(), bc_type="periodic")
    q_new = q_new(t_out)
    q_new = q_new / np.linalg.norm(q_new, 2, 1)[:, None]
    q_new = torch.from_numpy(q_new).float()

    render_poses = []
    for i, (new_q, scale) in enumerate(zip(q_new, s_new)):
        new_q = new_q.unsqueeze(0)
        R = util.quat_to_rot(new_q)
        t = R[:, :, 2] * scale
        new_pose = torch.eye(4, dtype=torch.float32).unsqueeze(0)
        new_pose[:, :3, :3] = R
        new_pose[:, :3, 3] = t
        render_poses.append(new_pose)
    render_poses = torch.cat(render_poses, dim=0)
else:
    print("Using default (360 loop) camera trajectory")
    if args.radius == 0.0:
        radius = (z_near + z_far) * 0.5
        print("> Using default camera radius", radius)
    else:
        radius = args.radius

    # Use 360 pose sequence from NeRF
    render_poses = torch.stack(
        [
            util.pose_spherical(angle, args.elevation, radius)
            for angle in np.linspace(-180, 180, args.num_views + 1)[:-1]
        ],
        0,
    )  # (NV, 4, 4)

render_rays = util.gen_rays(
    render_poses,
    W,
    H,
    focal * args.scale,
    z_near,
    z_far,
    c=c * args.scale if c is not None else None,
).to(device=device)
# (NV, H, W, 8)

bounding_radius = torch.tensor(args.radius).to(device=device)

focal = focal.to(device=device)

source = torch.tensor(list(map(int, args.source.split())), dtype=torch.long)
NS = len(source)
random_source = NS == 1 and source[0] == -1
assert not (source >= NV).any()

if renderer.n_coarse < 64:
    # Ensure decent sampling resolution
    renderer.n_coarse = 64
    renderer.n_fine = 128

with torch.no_grad():
    print("Encoding source view(s)")
    if random_source:
        src_view = torch.randint(0, NV, (1,))
    else:
        src_view = source
    
    net.encode(
        images[src_view].unsqueeze(0),
        poses[src_view].unsqueeze(0).to(device=device),
        focal,
        c=c,
    )

    print("Rendering", args.num_views * H * W, "rays")
    all_rgb_fine = []
    all_rgb_env = []
    for rays in tqdm.tqdm(
        torch.split(render_rays.view(-1, 8), args.ray_batch_size, dim=0)
    ):
        B, _ = rays.shape
        uv_env = util.sample_spherical_uv(rays[None], bounding_radius, app_imgs, 223)

        # Some pixels might be really close together and use the same encoding
        uv_env = torch.cat(uv_env, dim=-1).reshape(-1, 2)
        unique_uv, inv_map = uv_env.unique(dim=0, return_inverse=True)
        unq_u = unique_uv[:, 0]
        unq_v = unique_uv[:, 1]
        unq_patches = util.uv_to_rgb_patches(app_imgs, (unq_u, unq_v), 223)
        unq_encs = ref_encoder(unq_patches)
        all_encs = torch.zeros(B, 512)

        # Render out our scene using these encodings per ray
        all_encs[inv_map] = unq_encs[inv_map]
        rgb, _ = render_par(rays[None], all_encs[None])

        # Save the general area we ended up harmonizing with
        offset = 223 // 2
        u_max = unq_u.max() + offset
        u_min = unq_u.min() + offset
        v_max = unq_v.max() + offset
        v_min = unq_v.min() + offset

        harm_area = app_imgs[:, :, v_min:v_max, u_min:u_max]

        # Resize the harmonized area to better fit in video frame
        _, _, Ha, Wa = harm_area.shape
        ratio_h = H / Ha
        ratio_w = W / Wa
        resize_ratio = min(ratio_h, ratio_w)

        Hr = int(Ha * resize_ratio)
        Wr = int(Wa * resize_ratio)
        harm_area = F.interpolate(harm_area, size=(Ha, Wa), mode="bilinear")

        # Pad if necessary
        pad_h = max(H - Hr, 0)
        pad_w = max(W - Wr, 0)
        zero_pad = ZeroPad2d((0, pad_w, pad_h, 0))
        harm_area = zero_pad(harm_area)

        all_rgb_fine.append(rgb[0])
        all_rgb_env.append(harm_area)
    _depth = None

    rgb_fine = torch.cat(all_rgb_fine)
    # rgb_fine (V*H*W, 3)
    rgb_env = torch.cat(all_rgb_env)
    frames = torch.cat((rgb_fine.view(-1, H, W, 3), rgb_env), dim=-1)

print("Writing video")
vid_name = "{:04}".format(args.subset)
if args.split == "train":
    vid_name = "t" + vid_name
elif args.split == "val":
    vid_name = "v" + vid_name
vid_name += "_v" + "_".join(map(lambda x: "{:03}".format(x), source))
vid_path = os.path.join(args.visual_path, args.name, "video" + vid_name + ".mp4")
viewimg_path = os.path.join(
    args.visual_path, args.name, "video" + vid_name + "_view.jpg"
)
imageio.mimwrite(
    vid_path, (frames.cpu().numpy() * 255).astype(np.uint8), fps=args.fps, quality=8
)

img_np = (data["images"][src_view].permute(0, 2, 3, 1) * 0.5 + 0.5).numpy()
img_np = (img_np * 255).astype(np.uint8)
img_np = np.hstack((*img_np,))
imageio.imwrite(viewimg_path, img_np)

print("Wrote to", vid_path, "view:", viewimg_path)
