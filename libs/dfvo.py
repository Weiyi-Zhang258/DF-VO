# Copyright (C) Huangying Zhan 2019. All rights reserved.
# This software is licensed under the terms in the LICENSE file 
# which allows for non-commercial use only.

import cv2
import copy
from glob import glob
import math
from matplotlib import pyplot as plt
import numpy as np
import os
from time import time
from tqdm import tqdm

from libs.geometry.camera_modules import SE3
import libs.datasets as Dataset
from libs.deep_models.deep_models import DeepModel
from libs.general.frame_drawer import FrameDrawer
from libs.general.timer import Timer
from libs.matching.keypoint_sampler import KeypointSampler
from libs.matching.depth_consistency import DepthConsistency
from libs.tracker import EssTracker, PnpTracker
from libs.general.utils import *

from libs.tracker.gric import *


def get_E_from_pose(pose):
    R = pose.R
    t = pose.t / np.linalg.norm(pose.t)
    t_ssym = np.zeros((3,3))
    t_ssym[0, 1] = - t[2,0]
    t_ssym[0, 2] = t[1,0]
    t_ssym[1, 2] = - t[0,0]
    t_ssym[2, 1] = t[0,0]
    t_ssym[2, 0] = - t[1,0]
    t_ssym[1, 0] = t[2,0]

    E = t_ssym @ R
    return E



class DFVO():
    def __init__(self, cfg):
        """
        Args:
            cfg (edict): configuration reading from yaml file
        """
        # configuration
        self.cfg = cfg

        # tracking stage
        self.tracking_stage = 0

        # predicted global poses
        self.global_poses = {0: SE3()}

        # window size and keyframe step
        self.window_size = 2
        self.keyframe_step = 1
        
        # reference data and current data
        self.initialize_data()

        self.setup()

    def setup(self):
        """Reading configuration and setup, including
        - Tracking method
        - dataset
        - Keypoint Sampler
        - Deep networks
        - Deep layers
        - Display drawer
        - Timer
        """

        # intialize dataset
        datasets = {
            "kitti_odom": Dataset.KittiOdom,
            "kitti_raw": Dataset.KittiRaw,
            "tum-1": Dataset.TUM,
            "tum-2": Dataset.TUM,
            "tum-3": Dataset.TUM,
            "adelaide1": Dataset.Adelaide1,
            "adelaide2": Dataset.Adelaide2
        }
        self.dataset = datasets[self.cfg.dataset](self.cfg)

        # get tracking method
        self.tracking_method = self.get_tracking_method(self.cfg.tracking_method)
        self.initialize_tracker()

        # initialize keypoint sampler
        self.kp_sampler = KeypointSampler(self.cfg)
        
        # Deep networks
        self.deep_models = DeepModel(self.cfg)
        self.deep_models.initialize_models()
        
        # Depth consistency
        if self.cfg.kp_selection.depth_consistency.enable:
            self.depth_consistency_computer = DepthConsistency(self.cfg, self.dataset.cam_intrinsics)

        # visualization interface
        self.drawer = FrameDrawer(self.cfg.visualization)
        self.drawer.get_traj_init_xy(
                        vis_h=self.drawer.h,
                        vis_w=self.drawer.w/5*2,
                        gt_poses=self.dataset.gt_poses)
        
        # timer
        self.timers = Timer()

    def initialize_data(self):
        self.ref_data = {
                        'id': None, 
                        'timestamp': None,
                        'img': None,
                        'depth': None,
                        'raw_depth': None,
                        'pose': None,
                        # 'kp': None,
                        'kp_best': None,
                        'kp_list': None,
                        'flow': None,  # from ref->cur
                        'flow_diff': None,  # flow-consistency-error of ref->cur
                        'inliers': None
                        }
        self.cur_data = {
                        'id': None,
                        'timestamp': None,
                        'img': None,
                        'depth': None,
                        'pose': None,
                        # 'kp': None,
                        'kp_best': None,
                        'kp_list': None,
                        'flow': None,  # from cur->ref
                        }

    def get_tracking_method(self, method_idx):
        """Get tracking method
        Args:
            method_idx (int): tracking method index
                - 0: 2d-2d
                - 1: PnP
                - 2: 3d-3d
                - 3: hybrid
        Returns:
            track_method (str): tracking method
        """
        tracking_method_cases = {
            0: "2d-2d",
            1: "PnP",
            2: "3d-3d",
            3: "hybrid"
        }
        return tracking_method_cases[method_idx]

    def initialize_tracker(self):
        if self.tracking_method == "hybrid":
            self.e_tracker = EssTracker(self.cfg, self.dataset.cam_intrinsics, self)
            self.pnp_tracker = PnpTracker(self.cfg, self.dataset.cam_intrinsics)
        elif self.tracking_method == "PnP":
            self.pnp_tracker = PnpTracker(self.cfg, self.dataset.cam_intrinsics)
        else:
            assert False, "Wrong tracker is selected"

    def update_global_pose(self, new_pose, scale):
        """update estimated poses w.r.t global coordinate system
        Args:
            new_pose (SE3)
            scale (float): scaling factor
        """
        self.cur_data['pose'].t = self.cur_data['pose'].R @ new_pose.t * scale \
                            + self.cur_data['pose'].t
        self.cur_data['pose'].R = self.cur_data['pose'].R @ new_pose.R
        self.global_poses[self.cur_data['id']] = copy.deepcopy(self.cur_data['pose'])

    def tracking(self):
        """Tracking using both Essential matrix and PnP
        Essential matrix for rotation and translation direction;
            *** triangluate depth v.s. CNN-depth for translation scale ***
        PnP if Essential matrix fails
        """
        # First frame
        if self.tracking_stage == 0:
            # initial pose
            if self.cfg.directory.gt_pose_dir is not None:
                self.cur_data['pose'] = SE3(self.dataset.gt_poses[self.cur_data['id']])
            else:
                self.cur_data['pose'] = SE3()
            self.tracking_stage = 1
            return

        # Second to last frames
        elif self.tracking_stage >= 1:
            # Depth consistency (CNN depths + CNN pose)
            if self.cfg.kp_selection.depth_consistency.enable:
                self.depth_consistency_computer.compute(self.cur_data, self.ref_data)

            # kp_selection
            self.timers.start('kp_sel', 'tracking')
            kp_sel_outputs = self.kp_sampler.kp_selection(self.cur_data, self.ref_data, self.e_tracker)
            self.kp_sampler.update_kp_data(self.cur_data, self.ref_data, kp_sel_outputs)
            self.timers.end('kp_sel')

            ''' Pose estimation '''
            # Initialize hybrid pose
            hybrid_pose = SE3()
            E_pose = SE3()

            if self.tracking_method in ['hybrid']:
                # Essential matrix pose
                self.timers.start('E-tracker', 'tracking')
                e_tracker_outputs = self.e_tracker.compute_pose_2d2d(
                                self.cur_data[self.cfg.compute_2d2d_pose.kp_src],
                                self.ref_data[self.cfg.compute_2d2d_pose.kp_src]) # pose: from cur->ref
                E_pose = e_tracker_outputs['pose']
                self.timers.end('E-tracker')

                # Rotation
                hybrid_pose.R = E_pose.R

                # save inliers
                self.ref_data['inliers'] = e_tracker_outputs['inliers']

                # scale recovery
                if np.linalg.norm(E_pose.t) != 0:
                    # FIXME: for DOM
                    self.e_tracker.cnt = self.cur_data['id']

                    self.timers.start('scale_recovery', 'tracking')
                    scale_out = self.e_tracker.scale_recovery(self.cur_data, self.ref_data, E_pose)
                    scale = scale_out['scale']
                    if self.cfg.translation_scale.kp_src == 'kp_depth':
                        self.cur_data['kp_depth'] = scale_out['cur_kp_depth']
                        self.ref_data['kp_depth'] = scale_out['ref_kp_depth']
                        self.cur_data['valid_mask'] *= scale_out['rigid_flow_mask']
                    if scale != -1:
                        hybrid_pose.t = E_pose.t * scale
                    self.timers.end('scale_recovery')

            if self.tracking_method in ['PnP', 'hybrid']:
                # PnP if Essential matrix fail
                if np.linalg.norm(E_pose.t) == 0 or scale == -1:
                    self.timers.start('pnp', 'tracking')
                    pnp_outputs = self.pnp_tracker.compute_pose_3d2d(
                                    self.cur_data[self.cfg.PnP.kp_src],
                                    self.ref_data[self.cfg.PnP.kp_src],
                                    self.ref_data['depth']
                                    ) # pose: from cur->ref
                    self.timers.end('pnp')

                    # use PnP pose instead of E-pose
                    hybrid_pose = pnp_outputs['pose']
                    self.tracking_mode = "PnP"

            self.ref_data['pose'] = copy.deepcopy(hybrid_pose)

            # update global poses
            pose = self.ref_data['pose']

            # FIXME: testing only
            # print(pose.pose)
            self.update_global_pose(pose, 1)

            self.tracking_stage += 1

    def update_ref_data(self, ref_data, cur_data, window_size, kf_step=1):
        """Update reference data
        Args:
            ref_data (dict): reference data
                - e.g.
                    ref_data:
                    {
                        id: [0, 1, 2]
                        img: {0: I0, 1:I1, 2:I2}
                        ...
                    }
            cur_data (dict): current data
                - e.g.
                    cur_data:
                    {
                        id: 3
                        img: I3
                        ...
                    }
            cur_id (int): current image id
            window_size (int): number of frames in the window
        Returns:
            ref_data (dict): reference data
        """
        for key in cur_data:
            if key == "id":
                ref_data['id'] = cur_data['id']
                # if len(ref_data['id']) > window_size - 1:
                #     del(ref_data['id'][0])
            else:
                if ref_data.get(key, -1) is -1:
                    ref_data[key] = {}
                ref_data[key] = cur_data[key]
                # if len(ref_data[key]) > window_size - 1:
                #     drop_id = np.min(list(ref_data[key].keys()))
                #     del(ref_data[key][drop_id])
        
        # Delete unused flow to avoid data leakage
        ref_data['flow'] = {}
        cur_data['flow'] = {}
        ref_data['flow_diff'] = {}
        return ref_data, cur_data

    def load_raw_data(self):
        """load image data and (optional) GT/precomputed depth data
        """
        # Reading image
        self.cur_data['img'] = self.dataset.get_image(self.cur_data['timestamp'])

        # Reading/Predicting depth
        if self.dataset.data_dir['depth_src'] is not None:
            self.cur_data['raw_depth'] = self.dataset.get_depth(self.cur_data['timestamp'])
    
    def deep_model_inference(self):
        """deep model prediction
        """
        # Single-view Depth prediction
        if self.dataset.data_dir['depth_src'] is None:
            self.timers.start('depth_cnn', 'deep inference')
            self.cur_data['raw_depth'] = \
                    self.deep_models.depth.inference(img=self.cur_data['img'])
            self.cur_data['raw_depth'] = cv2.resize(self.cur_data['raw_depth'],
                                                (self.cfg.image.width, self.cfg.image.height),
                                                interpolation=cv2.INTER_NEAREST
                                                )
            self.timers.end('depth_cnn')
        self.cur_data['depth'] = preprocess_depth(self.cur_data['raw_depth'], self.cfg.crop.depth_crop, [self.cfg.depth.min_depth, self.cfg.depth.max_depth])

        # Two-view flow
        if self.tracking_stage >= 1:
            self.timers.start('flow_cnn', 'deep inference')
            flows = self.deep_models.forward_flow(
                                    self.cur_data,
                                    self.ref_data,
                                    forward_backward=self.cfg.deep_flow.forward_backward)
            
            # Store flow
            self.ref_data['flow'] = flows[(self.ref_data['id'], self.cur_data['id'])].copy()
            if self.cfg.deep_flow.forward_backward:
                self.cur_data['flow'] = flows[(self.cur_data['id'], self.ref_data['id'])].copy()
                self.ref_data['flow_diff'] = flows[(self.ref_data['id'], self.cur_data['id'], "diff")].copy()
            
            self.timers.end('flow_cnn')
        
        # Relative camera pose
        if self.tracking_stage >= 1 and self.cfg.pose_net.enable:
            self.timers.start('pose_cnn', 'deep inference')
            # Deep pose prediction
            self.ref_data['deep_pose'] = {}
            # pose prediction
            pose = self.deep_models.pose.inference(
                            self.ref_data['img'],
                            self.cur_data['img'], 
                            )
            self.ref_data['deep_pose'] = pose[0] # from cur->ref
            self.timers.end('pose_cnn')

    def main(self):
        print("==> Start DF-VO")

        if self.cfg.no_confirm:
            start_frame = 0
        else:
            start_frame = int(input("Start with frame: "))

        # FIXME: testing only
        # for img_id in tqdm(range(start_frame, 3)):
        for img_id in tqdm(range(start_frame, len(self.dataset), self.cfg.frame_step)):
            self.timers.start('DF-VO')
            self.tracking_mode = "Ess. Mat."

            """ Data reading """
            # Initialize ids and timestamps
            self.cur_data['id'] = img_id
            self.cur_data['timestamp'] = self.dataset.get_timestamp(img_id)

            # Read image data and (optional) precomputed depth data
            self.timers.start('data_loading')
            self.load_raw_data()
            self.timers.end('data_loading')

            # Deep model inferences
            self.timers.start('deep_inference')
            self.deep_model_inference()
            self.timers.end('deep_inference')

            """ Visual odometry """
            self.timers.start('tracking')
            self.tracking()
            self.timers.end('tracking')

            """ Visualization """
            if self.cfg.visualization.enable:
                self.timers.start('visualization')
                self.drawer.main(self)
                self.timers.end('visualization')

            """ Update reference and current data """
            self.ref_data, self.cur_data = self.update_ref_data(
                                    self.ref_data,
                                    self.cur_data,
                                    self.window_size,
                                    self.keyframe_step
            )

            self.timers.end('DF-VO')

        print("=> Finish!")

        """ Display & Save result """
        print("The result is saved in [{}].".format(self.cfg.result_dir))
        # Save trajectory map
        print("Save VO map.")
        map_png = "{}/map.png".format(self.cfg.result_dir)
        cv2.imwrite(map_png, self.drawer.data['traj'])

        # Save trajectory txt
        traj_txt = "{}/{}.txt".format(self.cfg.result_dir, self.cfg.seq)
        self.dataset.save_result_traj(traj_txt, self.global_poses)

        # Output experiement information
        self.timers.time_analysis()

