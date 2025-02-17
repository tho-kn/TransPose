import torch.nn
from torch.nn.functional import relu
from config import *
import articulate as art
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence


class RNN(torch.nn.Module):
    r"""
    An RNN Module including a linear input layer, an RNN, and a linear output layer.
    """
    def __init__(self, n_input, n_output, n_hidden, n_rnn_layer=2, bidirectional=True, dropout=0.2):
        super(RNN, self).__init__()
        self.rnn = torch.nn.LSTM(n_hidden, n_hidden, n_rnn_layer, batch_first=True, bidirectional=bidirectional)
        self.linear1 = torch.nn.Linear(n_input, n_hidden)
        self.linear2 = torch.nn.Linear(n_hidden * (2 if bidirectional else 1), n_output)
        self.dropout = torch.nn.Dropout(dropout)

    def forward(self, x, h=None, seq_lengths=None):
        # seq_lengths for batched forward
        x = relu(self.linear1(self.dropout(x))) #.unsqueeze(1) This might be necessary
        if seq_lengths is not None:
            x = pack_padded_sequence(x, seq_lengths, batch_first=True, enforce_sorted=False)
        x, h = self.rnn(x, h)
        if seq_lengths is not None:
            x, _ = pad_packed_sequence(x, batch_first=True)
        # return self.linear2(x.squeeze(1)), h
        return self.linear2(x), h


class TransPoseNet(torch.nn.Module):
    r"""
    Whole pipeline for pose and translation estimation.
    """
    def __init__(self, num_past_frame=20, num_future_frame=5, hip_length=None, upper_leg_length=None,
                 lower_leg_length=None, prob_threshold=(0.5, 0.9), gravity_velocity=-0.018, joint_mask=torch.tensor([1, 2, 16, 17]), is_train=False):
        r"""
        :param num_past_frame: Number of past frames for a biRNN window.
        :param num_future_frame: Number of future frames for a biRNN window.
        :param hip_length: Hip length in meters. SMPL mean length is used by default. Float or tuple of 2.
        :param upper_leg_length: Upper leg length in meters. SMPL mean length is used by default. Float or tuple of 2.
        :param lower_leg_length: Lower leg length in meters. SMPL mean length is used by default. Float or tuple of 2.
        :param prob_threshold: The probability threshold used to control the fusion of the two translation branches.
        :param gravity_velocity: The gravity velocity added to the Trans-B1 when the body is not on the ground.
        """
        super().__init__()
        n_imu = 6 * 3 + 6 * 9   # acceleration (vector3) and rotation matrix (matrix3x3) of 6 IMUs
        self.pose_s1 = RNN(n_imu,                         joint_set.n_leaf * 3,       256)
        self.pose_s2 = RNN(joint_set.n_leaf * 3 + n_imu,  joint_set.n_full * 3,       64)
        self.pose_s3 = RNN(joint_set.n_full * 3 + n_imu,  joint_set.n_reduced * 6,    128)
        self.tran_b1 = RNN(joint_set.n_leaf * 3 + n_imu,  2,                          64)
        self.tran_b2 = RNN(joint_set.n_full * 3 + n_imu,  3,                          256,    bidirectional=False)

        # lower body joint
        m = art.ParametricModel(paths.smpl_file)
        j, _ = m.get_zero_pose_joint_and_vertex()
        b = art.math.joint_position_to_bone_vector(j[joint_set.lower_body].unsqueeze(0),
                                                   joint_set.lower_body_parent).squeeze(0)
        bone_orientation, bone_length = art.math.normalize_tensor(b, return_norm=True)
        if hip_length is not None:
            bone_length[1:3] = torch.tensor(hip_length)
        if upper_leg_length is not None:
            bone_length[3:5] = torch.tensor(upper_leg_length)
        if lower_leg_length is not None:
            bone_length[5:7] = torch.tensor(lower_leg_length)
        b = bone_orientation * bone_length
        b[:3] = 0

        # constant
        self.global_to_local_pose = m.inverse_kinematics_R
        self.lower_body_bone = b
        self.num_past_frame = num_past_frame
        self.num_future_frame = num_future_frame
        self.num_total_frame = num_past_frame + num_future_frame + 1
        self.prob_threshold = prob_threshold
        self.gravity_velocity = torch.tensor([0, gravity_velocity, 0])
        self.feet_pos = j[10:12].clone()
        self.floor_y = j[10:12, 1].min().item()
        self.joint_mask = joint_mask

        self.is_train = is_train

        # variable
        self.rnn_state = None
        self.imu = None
        self.current_root_y = 0
        self.last_lfoot_pos, self.last_rfoot_pos = self.feet_pos
        self.last_root_pos = torch.zeros(3)
        self.reset()

        if not self.is_train:
            self.load_state_dict(torch.load(paths.weights_file))
            self.eval()

    def _reduced_glb_6d_to_full_local_mat(self, root_rotation, glb_reduced_pose):
        glb_reduced_pose = art.math.r6d_to_rotation_matrix(glb_reduced_pose).view(-1, joint_set.n_reduced, 3, 3)
        global_full_pose = torch.eye(3, device=glb_reduced_pose.device).repeat(glb_reduced_pose.shape[0], 24, 1, 1)
        global_full_pose[:, joint_set.reduced] = glb_reduced_pose
        pose = self.global_to_local_pose(global_full_pose).view(-1, 24, 3, 3)
        pose[:, joint_set.ignored] = torch.eye(3, device=pose.device)
        pose[:, 0] = root_rotation.view(-1, 3, 3)
        return pose

    def _prob_to_weight(self, p):
        return (p.clamp(self.prob_threshold[0], self.prob_threshold[1]) - self.prob_threshold[0]) / \
               (self.prob_threshold[1] - self.prob_threshold[0])

    def reset(self):
        r"""
        Reset online forward states.
        """
        self.rnn_state = None
        self.imu = None
        self.current_root_y = 0
        self.last_lfoot_pos, self.last_rfoot_pos = self.feet_pos
        self.last_root_pos = torch.zeros(3)

    def forward(self, imu, rnn_state=None, seq_lengths=None):
        self.leaf_joint_position = self.pose_s1.forward(imu, seq_lengths=seq_lengths)[0]
        self.full_joint_position = self.pose_s2.forward(torch.cat((self.leaf_joint_position, imu), dim=-1), seq_lengths=seq_lengths)[0]
        self.global_reduced_pose = self.pose_s3.forward(torch.cat((self.full_joint_position, imu), dim=-1), seq_lengths=seq_lengths)[0]
        self.contact_probability = self.tran_b1.forward(torch.cat((self.leaf_joint_position, imu), dim=-1), seq_lengths=seq_lengths)[0]
        self.velocity, self.rnn_state = self.tran_b2.forward(torch.cat((self.full_joint_position, imu), dim=-1), rnn_state, seq_lengths=seq_lengths)
        return self.leaf_joint_position, self.full_joint_position, self.global_reduced_pose, self.contact_probability, self.velocity, self.rnn_state

    def forward_offline(self, imu, seq_lengths=None):
        r"""
        Offline forward.

        :param imu: Tensor in shape [num_frame, input_dim(6 * 3 + 6 * 9)].
        :param seq_lengths: 1D Tensor for lengths of input sequences.
        :return: Pose tensor in shape [num_frame, 24, 3, 3] and translation tensor in shape [num_frame, 3].
        """

        _, _, global_reduced_pose, contact_probability, velocity, _ = self.forward(imu, seq_lengths=seq_lengths)

        # calculate pose (local joint rotation matrices)
        # flatten forward results here, for batch operation
        root_rotation = imu[..., -9:].view(-1, 3, 3)
        velocity = velocity.view(-1, velocity.shape[-1])
        contact_probability = contact_probability.view(-1, contact_probability.shape[-1])
        global_reduced_pose = global_reduced_pose.view(-1, global_reduced_pose.shape[-1])

        pose = self._reduced_glb_6d_to_full_local_mat(root_rotation.cpu(), global_reduced_pose.cpu())

        # calculate velocity (translation between two adjacent frames in 60fps in world space)
        j = art.math.forward_kinematics(pose[:, joint_set.lower_body],
                                        self.lower_body_bone.expand(pose.shape[0], -1, -1),
                                        joint_set.lower_body_parent)[1]
        lerp1 = torch.cat((torch.zeros(1, 3, device=j.device), j[:-1, 7] - j[1:, 7]))
        lerp2 = torch.cat((torch.zeros(1, 3, device=j.device), j[:-1, 8] - j[1:, 8]))
        tran_b1_vel = self.gravity_velocity + art.math.lerp(lerp1, lerp2, contact_probability.max(dim=1).indices.view(-1, 1).cpu())
        tran_b2_vel = root_rotation.bmm(velocity.unsqueeze(-1)).squeeze(-1).cpu() * vel_scale / 60   # to world space
        weight = self._prob_to_weight(contact_probability.cpu().max(dim=1).values.sigmoid()).view(-1, 1)
        velocity = art.math.lerp(tran_b2_vel, tran_b1_vel, weight)

        # remove penetration
        current_root_y = 0
        for i in range(velocity.shape[0]):
            current_foot_y = current_root_y + j[i, 7:9, 1].min().item()
            if current_foot_y + velocity[i, 1].item() <= self.floor_y:
                velocity[i, 1] = self.floor_y - current_foot_y
            current_root_y += velocity[i, 1].item()

        if len(imu.shape) == 3:
            batch_size = imu.shape[0]
            sequence_length = imu.shape[1]
            pose = pose.reshape(batch_size, sequence_length, 24, 3, 3)
            velocity = velocity.reshape(batch_size, sequence_length, 3)

        position = self.velocity_to_root_position(velocity)

        return pose, position

    def forward_online(self, x):
        r"""
        Online forward.

        :param x: A tensor in shape [input_dim(6 * 3 + 6 * 9)].
        :return: Pose tensor in shape [24, 3, 3] and translation tensor in shape [3].
        """
        imu = x.repeat(self.num_total_frame, 1) if self.imu is None else torch.cat((self.imu[1:], x.view(1, -1)))
        _, _, global_reduced_pose, contact_probability, velocity, self.rnn_state = \
            self.forward(imu, rnn_state=self.rnn_state)
        contact_probability = contact_probability[self.num_past_frame].sigmoid().view(-1).cpu()

        # calculate pose (local joint rotation matrices)
        root_rotation = imu[self.num_past_frame, -9:].view(3, 3).cpu()
        global_reduced_pose = global_reduced_pose[self.num_past_frame].cpu()
        pose = self._reduced_glb_6d_to_full_local_mat(root_rotation, global_reduced_pose).squeeze(0)

        # calculate velocity (translation between two adjacent frames in 60fps in world space)
        lfoot_pos, rfoot_pos = art.math.forward_kinematics(pose[joint_set.lower_body].unsqueeze(0),
                                                           self.lower_body_bone.unsqueeze(0),
                                                           joint_set.lower_body_parent)[1][0, 7:9]
        if contact_probability[0] > contact_probability[1]:
            tran_b1_vel = self.last_lfoot_pos - lfoot_pos + self.gravity_velocity
        else:
            tran_b1_vel = self.last_rfoot_pos - rfoot_pos + self.gravity_velocity
        tran_b2_vel = root_rotation.mm(velocity[self.num_past_frame].cpu().view(3, 1)).view(3) / 60 * vel_scale
        weight = self._prob_to_weight(contact_probability.max())
        velocity = art.math.lerp(tran_b2_vel, tran_b1_vel, weight)

        # remove penetration
        current_foot_y = self.current_root_y + min(lfoot_pos[1].item(), rfoot_pos[1].item())
        if current_foot_y + velocity[1].item() <= self.floor_y:
            velocity[1] = self.floor_y - current_foot_y

        self.current_root_y += velocity[1].item()
        self.last_lfoot_pos, self.last_rfoot_pos = lfoot_pos, rfoot_pos
        self.imu = imu
        self.last_root_pos += velocity
        return pose, self.last_root_pos.clone()
        
    def compute_loss_s1(self, gt_leaf_joint_position):
        """
        Compute loss from S1
        
        MeanSquareError for the root-relative positions of the leaf joints
        """
        return torch.nn.MSELoss()(self.leaf_joint_position, gt_leaf_joint_position)
        
    def compute_loss_s2(self, gt_joint_position):
        """
        Compute loss from S2
        
        MeanSquareError for the root-relative positions of all joints
        """
        return torch.nn.MSELoss()(self.full_joint_position, gt_joint_position)
        
    def compute_loss_s3(self, gt_joint_rotation):
        """
        Compute loss from S3
        
        MeanSquareError for the ratation of all joints in 6D representation
        """
        return torch.nn.MSELoss()(self.global_reduced_pose, gt_joint_rotation)
        
    def compute_loss_b1(self, gt_contact_probability):
        # Both feet are included in contact_probability
        # cross-entropy loss to get the foot contact probability
        output = self.contact_probability
        loss = gt_contact_probability * torch.log(output) + (1.0 - gt_contact_probability) * torch.log(1.0 - output)
        loss = loss * -1.0
        return loss

    def compute_loss_vel(self, gt_velocity, frame_range=1):
        t = self.velocity.shape[0]
        v_dim = self.velocity.shape[1]
        num_windows = t // frame_range
        start_frame = torch.randint(t - num_windows * frame_range) if t % frame_range != 0 else 0

        end_frame = t - t % frame_range + start_frame
        velocity_trim = self.velocity[start_frame:end_frame].reshape(-1, frame_range, v_dim)
        gt_velocity_trim = gt_velocity[start_frame:end_frame].reshape(-1, frame_range, v_dim)

        frame_velocity = torch.sum(velocity_trim, dim=1)
        gt_frame_velocity = torch.sum(gt_velocity_trim, dim=1)

        return torch.nn.MSELoss(reduction='sum')(frame_velocity, gt_frame_velocity)

    def compute_loss_b2(self, gt_vel):
        # something related to velocity
        # check shape of the input (temporal dimension)
        loss = sum([self.compute_loss_vel(gt_vel, i) for i in [1, 3, 9, 27]])
        return loss
    
    def compute_loss(self, gt_joint_position, gt_joint_rotation, gt_contact_probability, gt_velocity):
        loss_s1 = self.compute_loss_s1(gt_joint_position[self.joint_mask])
        loss_s2 = self.compute_loss_s2(gt_joint_position)
        loss_s3 = self.compute_loss_s3(gt_joint_rotation)
        
        loss_b1 = self.compute_loss_b1(gt_contact_probability)
        loss_b2 = self.compute_loss_b2(gt_velocity)
        
        self.loss_details = [loss_s1, loss_s2, loss_s3, loss_b1, loss_b2]

        return sum(self.loss_details)
        
    def set_loss_names(self):
        self.loss_names = ['s1', 's2', 's3', 'b1', 'b2']

    @staticmethod
    def velocity_to_root_position(velocity):
        """
        Change velocity to root position. (not optimized)

        :param velocity: Velocity tensor in shape [..., 3].
        :return: Translation tensor in shape [..., 3] for root positions.
        """

        # return torch.stack([velocity[:i+1].sum(dim=0) for i in range(velocity.shape[0])])
        return torch.cumsum(velocity, dim=-2)
