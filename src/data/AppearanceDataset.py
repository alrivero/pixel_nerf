import os
import torch
import torch.nn.functional as F
import glob
import imageio
import random
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
        img_ind=None
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
        self.img_ind = img_ind
    
    def __len__(self):
        return len(self.all_objs)

    def __getitem__(self, index):
        cat, root_dir = self.all_objs[index]

        rgb_paths = [
            x
            for x in glob.glob(os.path.join(root_dir, "images", "dslr_images_undistorted", "*"))
            if (x.endswith(".JPG") or x.endswith(".PNG"))
        ]
        rgb_paths = sorted(rgb_paths)
        
        # Get image from this directory
        img_ind = self.img_ind if self.img_ind < len(rgb_paths) is not None else 0
        img = imageio.imread(rgb_paths[img_ind])[..., :3]
        img_tensor = self.image_to_tensor(img)

        # NOTE: Right now, no intrisic or extrinsic camera information is being used here!
        # Add it later in necessary! (Refer to DVRDataset)

        if self.image_size is not None and img_tensor.shape[-2:] != self.image_size:
            img_tensor = torch.unsqueeze(img_tensor, 0)
            img_tensor = F.interpolate(img_tensor, size=self.image_size, mode="area")
            img_tensor = torch.squeeze(img_tensor, 0)
        
        result = {
            "path": rgb_paths[img_ind],
            "img_id": index,
            "images": img_tensor,
        }

        return result
