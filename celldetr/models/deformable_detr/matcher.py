# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Modules to compute the matching cost and solve the corresponding LSAP.
"""
import torch
from scipy.optimize import linear_sum_assignment
from torch import nn

from ...util.moment_ops import kl_divergence_batched, moments_to_cov, denormalize_moments

class HungarianMatcher(nn.Module):
    """This class computes an assignment between the targets and the predictions of the network

    For efficiency reasons, the targets don't include the no_object. Because of this, in general,
    there are more predictions than targets. In this case, we do a 1-to-1 matching of the best predictions,
    while the others are un-matched (and thus treated as non-objects).
    """

    def __init__(self,
                 cost_class: float = 1,
                 cost_moments: float = 1,
                 cost_kl: float = 1):
        """Creates the matcher

        Params:
            cost_class: This is the relative weight of the classification error in the matching cost
            cost_bbox: This is the relative weight of the L1 error of the bounding box coordinates in the matching cost
            cost_giou: This is the relative weight of the giou loss of the bounding box in the matching cost
        """
        super().__init__()
        self.cost_class = cost_class
        self.cost_moments = cost_moments
        self.cost_kl = cost_kl
        assert cost_class != 0 or cost_moments != 0 or cost_kl != 0, "all costs cant be 0"

    def forward(self, outputs, targets):
        """ Performs the matching

        Params:
            outputs: This is a dict that contains at least these entries:
                 "pred_logits": Tensor of dim [batch_size, num_queries, num_classes] with the classification logits
                 "pred_boxes": Tensor of dim [batch_size, num_queries, 4] with the predicted box coordinates

            targets: This is a list of targets (len(targets) = batch_size), where each target is a dict containing:
                 "labels": Tensor of dim [num_target_boxes] (where num_target_boxes is the number of ground-truth
                           objects in the target) containing the class labels
                 "boxes": Tensor of dim [num_target_boxes, 4] containing the target box coordinates

        Returns:
            A list of size batch_size, containing tuples of (index_i, index_j) where:
                - index_i is the indices of the selected predictions (in order)
                - index_j is the indices of the corresponding selected targets (in order)
            For each batch element, it holds:
                len(index_i) = len(index_j) = min(num_queries, num_target_boxes)
        """
        with torch.no_grad():
            bs, num_queries = outputs["pred_logits"].shape[:2]

            # We flatten to compute the cost matrices in a batch
            out_prob = outputs["pred_logits"].flatten(0, 1).sigmoid()
            out_moments = outputs["pred_moments"].flatten(0, 1)  # [batch_size * num_queries, 5]

            # Also concat the target labels and boxes
            tgt_ids = torch.cat([v["labels"] for v in targets])
            tgt_moments = torch.cat([v["moments"] for v in targets])

            # Compute the classification cost.
            alpha = 0.25
            gamma = 2.0
            neg_cost_class = (1 - alpha) * (out_prob ** gamma) * (-(1 - out_prob + 1e-8).log())
            pos_cost_class = alpha * ((1 - out_prob) ** gamma) * (-(out_prob + 1e-8).log())
            cost_class = pos_cost_class[:, tgt_ids] - neg_cost_class[:, tgt_ids]

            # Compute the L1 cost between moments
            cost_moments = torch.cdist(out_moments, tgt_moments, p=1)
            
            denorm_out_moments = denormalize_moments(out_moments)
            denorm_tgt_moments = denormalize_moments(tgt_moments) 

            # Compute KL_divergence between Gaussian distributions extracted from moments
            mu_src = denorm_out_moments[:, :2]  #Extracting [cx, cy]
            mu_tgt = denorm_tgt_moments[:, :2]
            Sigma_src = moments_to_cov(denorm_out_moments[:, 2:]) #Extracting covariance matrices
            Sigma_tgt = moments_to_cov(denorm_tgt_moments[:, 2:])

            cost_kl = kl_divergence_batched(mu_src, Sigma_src, mu_tgt, Sigma_tgt)
            #TODO: Check if this is the right way to handle NaNs and Infs
            # IMPROVISATION: Substitute invalid values from the kl_cost matrix to high penalty value (100)
            cost_kl[cost_kl.isnan() | cost_kl.isinf()] = 1000

            # Final cost matrix
            C = self.cost_moments * cost_moments + self.cost_class * cost_class + self.cost_kl * cost_kl
            C = C.view(bs, num_queries, -1).cpu()

            sizes = [len(v["boxes"]) for v in targets]
            indices = [linear_sum_assignment(c[i]) for i, c in enumerate(C.split(sizes, -1))]
            return [(torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64)) for i, j in indices]
        
def build_matcher(cfg):
    return HungarianMatcher(cost_class=cfg.matcher.cost_class,
                            cost_moments=cfg.matcher.cost_moments,
                            cost_kl=cfg.matcher.cost_kl)
