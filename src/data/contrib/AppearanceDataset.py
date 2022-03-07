import os
import torch
import torch.nn.functional as F
import glob
import imageio
import numpy as np
import cv2
from util import get_image_to_tensor_balanced

class AppearanceDataset(torch.utils.data.Dataset):

    """
    Dataset consisting of images of a scene taken at different angles 
    Meant to be used with appearance encoder
    """
    def __init__(
        self,
        path,
        stage="train",
        list_prefix="new_",
        image_size=None,
        sub_format="eth3d",
        scale_focal=True,
        max_imgs=100000,
        z_near=1.2,
        z_far=4.0,
    ):
        super().__init__()
        self.base_path = path
        assert os.path.exists(self.base_path)

        # Get all directories in main directory
        cats = [x for x in glob.glob(os.path.join(path, "*")) if os.path.isdir(x)]

        if stage == "train":
            file_lists = [os.path.join(x, list_prefix + "train.lst") for x in cats]
        elif stage == "val":
            file_lists = [os.path.join(x, list_prefix + "val.lst") for x in cats]
        elif stage == "test":
            file_lists = [os.path.join(x, list_prefix + "test.lst") for x in cats]

        all_objs = []
        for file_list in file_lists:
            if not os.path.exists(file_list):
                continue
            base_dir = os.path.dirname(file_list)
            cat = os.path.basename(base_dir)
            with open(file_list, "r") as f:
                objs = [(cat, os.path.join(base_dir, x.strip())) for x in f.readlines()]
            all_objs.extend(objs)

        self.all_objs = all_objs
        self.stage = stage

        self.image_to_tensor = get_image_to_tensor_balanced()
        print(
            "Loading Appearance dataset",
            self.base_path,
            "stage",
            stage,
            len(self.all_objs),
            "objs",
            "type:",
            sub_format,
        )

        # NOTE: Right now, no intrisic or extrinsic camera information is being used here!
        # Add it later!

        self.image_size = image_size
        self.max_imgs = max_imgs
        self.lindisp = False
    
    def __len__(self):
        return len(self.all_objs)

    def __getitem__(self, index):
        cat, root_dir = self.all_objs[index]

        rgb_paths = [
            x
            for x in glob.glob(os.path.join(root_dir, "image", "*"))
            if (x.endswith(".jpg") or x.endswith(".png"))
        ]
        rgb_paths = sorted(rgb_paths)

        if len(rgb_paths) <= self.max_imgs:
            sel_indices = np.arange(len(rgb_paths))
        else:
            sel_indices = np.random.choice(len(rgb_paths), self.max_imgs, replace=False)
            rgb_paths = [rgb_paths[i] for i in sel_indices]

        # NOTE: Right now, no intrisic or extrinsic camera information is being used here!
        # Add it later! (Refer to DVRDataset)

        all_imgs = []
        for idx, rgb_path in enumerate(rgb_paths):
            i = sel_indices[idx]
            img = imageio.imread(rgb_path)[..., :3]
            img_tensor = self.image_to_tensor(img)

            # Tons of camera stuff happening here!

            all_imgs.append(img_tensor)
        all_imgs = torch.stack(all_imgs)

        if self.image_size is not None and all_imgs.shape[-2:] != self.image_size:
            all_imgs = F.interpolate(all_imgs, size=self.image_size, mode="area")

            # Camera stuff here too
        
        result = {
            "path": root_dir,
            "img_id": index,
            "images": all_imgs,
        }

        return result