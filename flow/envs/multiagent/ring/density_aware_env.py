
import numpy as np
from gym.spaces.box import Box
import random
from scipy.optimize import fsolve
from copy import deepcopy

import torch
import torch.nn as nn
from flow.core.params import InitialConfig
from flow.core.params import NetParams
from flow.envs.multiagent.base import MultiEnv
from flow.envs.ring.wave_attenuation import v_eq_max_function


ADDITIONAL_ENV_PARAMS = {
    # maximum acceleration of autonomous vehicles
    'max_accel': 1,
    # maximum deceleration of autonomous vehicles
    'max_decel': 1,
    # bounds on the ranges of ring road lengths the autonomous vehicle is
    # trained on
    'ring_length': [220, 270],
}

class MultiAgentDensityAwareRLEnv(MultiEnv):
    def __init__(self, env_params, sim_params, network, simulator='traci'):
        for p in ADDITIONAL_ENV_PARAMS.keys():
            if p not in env_params.additional_params:
                raise KeyError(
                    'Environment parameter \'{}\' not supplied'.format(p))

        super().__init__(env_params, sim_params, network, simulator)

        self.LOCAL_ZONE = 50 # m, arbitrarily set
        self.VEHICLE_LENGTH = 5 #m can use self.k.vehicle.get_length(veh_id)
        self.MAX_SPEED = 10 # m/s
        self.label_meaning = ["Leaving", "Forming", "Free Flow", "Congested", "Undefined", "No vehicle in front"] 
        # Set this on init and reset
        self.tse_model = self.load_tse_model()
        self.tse_output = None
        self.tse_output_encoded = None
        self.lead_rl_id = None

        self.leader_obs = None
        self.leader_agent = self.setup_trained_leader()

    @property
    def action_space(self):
        """See class definition."""
        return Box(
            low=-np.abs(self.env_params.additional_params['max_decel']),
            high=self.env_params.additional_params['max_accel'],
            shape=(1,),
            dtype=np.float32)

    @property
    def observation_space(self):
        """See class definition."""

        shp = (3 + 6,) # 6 categories of the one hot encoded TSE output
        return Box(low=-5, # WHY THIS?
                   high=5,  # INSTEAD OF -float('inf') and float('inf')?
                   shape=shp, 
                   dtype=np.float32)

    
    def _apply_rl_actions(self, rl_actions):
        """Split the accelerations"""

        print(f"rl_actions: {rl_actions} \n")
        if rl_actions:
            leader_id = self.k.vehicle.get_rl_ids()[-1]
            follower_ids = self.k.vehicle.get_rl_ids()[:-1]

            print(f"leader_id: {leader_id}, follower_ids: {follower_ids} \n")

            #leader_action = rl_actions[leader_id]
            follower_actions = [rl_actions[id] for id in follower_ids]
            
            # Hack this
            #print(f"\n\nHello:{self.leader_obs}\n\n")
            #leader_action = np.array([-1.0])
            leader_action = self.get_trained_leader_action()
            #print(f"Leader action: {leader_action}")

            self.k.vehicle.apply_acceleration(leader_id, leader_action)
            self.k.vehicle.apply_acceleration(follower_ids, follower_actions)
    
    def compute_reward(self, rl_actions, **kwargs):
            """
            Lead gets rewarded the same as the singleagent version 
            For maximum stability: others get rewarded for?
            """
            # in the warmup steps
            if rl_actions is None:
                return 0

            vel = np.array([
                self.k.vehicle.get_speed(veh_id)
                for veh_id in self.k.vehicle.get_ids()
            ])

            if any(vel < -100) or kwargs['fail']:
                return 0.
            
            rew = {}

            # reward for leader
            lead_rl_id = self.k.vehicle.get_rl_ids()[-1]

            reward_leader = 0.0
            rew.update({lead_rl_id : reward_leader})

            # reward for all followers
            follower_ids = self.k.vehicle.get_rl_ids()[:-1]


            for follower_id in follower_ids:
                
                # Speed of the follower
                follower_speed = self.k.vehicle.get_speed(follower_id)

                # Time headway of the follower
                lead_id = self.k.vehicle.get_leader(follower_id)
                # prevent division by zero
                front_speed = max(0.01, self.k.vehicle.get_speed(lead_id))
                front_distance = (self.k.vehicle.get_x_by_id(lead_id) - self.k.vehicle.get_x_by_id(follower_id)) % self.k.network.length()
                time_headway = front_distance/front_speed  # close approximation, assume zero instantaneous acceleration

                # Acceleration of the follower
                follower_accel_magnitude = np.abs(rl_actions[follower_id].item())

                reward_follow =  0.2 * follower_speed - 4 * follower_accel_magnitude #- 0.2*time_headway
                print(f"follower_id: {follower_id}, reward_follow: {reward_follow} \n")
                rew.update({follower_id : reward_follow})
            
            return rew
        

    # Helper 1
    def sort_vehicle_list(self, vehicles_in_zone):
        return sorted(vehicles_in_zone, key=lambda x: (x[:-1], -int(x[-1])) if len(x) > 1 else (x, 0))

    # Helper 3: Get TSE output
    def get_tse_output(self, current_obs):
        """
        Get the output of Traffic State Estimator Neural Network
        """
        current_obs = torch.from_numpy(current_obs).flatten()

        with torch.no_grad():
            outputs = self.tse_model(current_obs.unsqueeze(0))

        # print("TSE output: ", outputs)
        # return outputs.numpy() # Logits

        _, predicted_label = torch.max(outputs, 1)
        predicted_label = predicted_label.numpy()
        return predicted_label
        

    # Helper 4: Load TSE model 
    def load_tse_model(self, ):
        """
        Load the Traffic State Estimator Neural Network and its trained weights
        """
        class TSE_Net(nn.Module):
            def __init__(self, input_size, num_classes):
                super(TSE_Net, self).__init__() 
                self.fc1 = nn.Linear(input_size, 32)
                self.relu = nn.ReLU()
                self.fc2 = nn.Linear(32, 16)
                self.relu = nn.ReLU()
                self.fc3 = nn.Linear(16, num_classes)
                
            def forward(self, x):
                out = self.fc1(x)
                out = self.relu(out)
                out = self.fc2(out)
                out = self.relu(out)
                out = self.fc3(out)
                return out

        input_size = 10*2
        num_classes = 6
        url = "https://huggingface.co/matrix-multiply/Congestion_Stage_Estimator/resolve/main/best_cse_model.pt"
        saved_best_net = TSE_Net(input_size, num_classes)

        state_dict = torch.hub.load_state_dict_from_url(url)
        saved_best_net.load_state_dict(state_dict)
        saved_best_net.eval()

        return saved_best_net

    # Helper 5: Setup tained leader agent
    def setup_trained_leader(self, ):
        """
        Setup the trained leader agent
        """
        import ray
        try:
            from ray.rllib.agents.agent import get_agent_class
        except ImportError:
            from ray.rllib.agents.registry import get_agent_class
        from flow.utils.registry import make_create_env
        from flow.utils.rllib import get_flow_params, get_rllib_config
        from ray.tune.registry import register_env

        # Make the num cpu here one more than the one in training config file
        ray.init(num_cpus=5, ignore_reinit_error=True)

        result_dir_name = "/home/dh127-pc3/Desktop/flow/poudel_ring/Ours/Trained_policies/Single_agent/PPO_DensityAwareRLEnv-v0_2355c52c_2023-07-21_18-27-20qktx7e8o"
        checkpoint_num = "290" 

        result_dir = result_dir_name if result_dir_name[-1] != '/' else result_dir_name[:-1]
        checkpoint = result_dir + '/checkpoint_' + checkpoint_num
        checkpoint = checkpoint + '/checkpoint-' + checkpoint_num
        
        config = get_rllib_config(result_dir)
        config['num_workers'] = 0

        flow_params = get_flow_params(config)
        sim_params = flow_params['sim']
        setattr(sim_params, 'num_clients', 1)

        config_run = config['env_config']['run'] if 'run' in config['env_config'] else None
        agent_cls = get_agent_class(config_run)

        create_env, env_name = make_create_env(params=flow_params, version=0)
        register_env(env_name, create_env)

        agent = agent_cls(env=env_name, config=config)
        agent.restore(checkpoint)

        print(f"\n\nLeader agent restored\n{agent.get_policy()}")
        #ray.shutdown()
        return agent 

    # Helper 6: Get action from trained leader agent
    def get_trained_leader_action(self,):
        """
        Get the action from trained leader agent
        """
        action = self.leader_agent.compute_action(self.leader_obs)
        return action

    def get_state(self):
        """See class definition."""
        

        # Get TSE ouput
        # This is the way lead is assigned
        self.lead_rl_id = f"{self.k.vehicle.get_rl_ids()[-1]}"
        rl_pos = self.k.vehicle.get_x_by_id(self.lead_rl_id)
        current_length = self.k.network.length()

        # Get the list of all vehicles in the local zone (sorted from farthest to closest)
        vehicles_in_zone = self.sort_vehicle_list(self.k.vehicle.get_veh_list_local_zone(self.lead_rl_id, 
                                                                                         current_length, 
                                                                                         self.LOCAL_ZONE )) # Direction i front by default


        observation_tse = np.full((10, 2), -1.0)
        num_vehicle_in_zone = len(vehicles_in_zone)
        distances = []
        if num_vehicle_in_zone > 0:
            for i in range(len(vehicles_in_zone)):
                # Distance is measured center to center between the two vehicles (if -5 present, distance if bumper to bumper)
                rel_pos = (self.k.vehicle.get_x_by_id(vehicles_in_zone[i]) - rl_pos) % current_length
                norm_pos = rel_pos / self.LOCAL_ZONE # This is actually the normalized distance
                distances.append(norm_pos)

                vel = self.k.vehicle.get_speed(vehicles_in_zone[i])
                norm_vel = vel / self.MAX_SPEED

                observation_tse[i] = [norm_pos, norm_vel]
                
        observation_tse = np.array(observation_tse, dtype=np.float32)
        #print(f"Num vehicles in zone:{num_vehicle_in_zone} Observation TSE: {observation_tse}")

        self.tse_output = self.get_tse_output(observation_tse)
        self.tse_output_encoded = np.zeros(6) 
        self.tse_output_encoded[self.tse_output] = 1

        print(f"\nTSE output: {self.tse_output}, one hot encoded: {self.tse_output_encoded}, meaning: {self.label_meaning[self.tse_output[0]]}")

        obs = {}
        # For RL agents
        # All the agents observe thier leader only if its within the local zone
        for rl_id in self.k.vehicle.get_rl_ids():

            # Zone count will count the RL agent itself as well
            # Observe the leader
            if num_vehicle_in_zone > 1:
                lead_id = self.k.vehicle.get_leader(rl_id) or rl_id

                # normalizers
                max_speed = 15.
                max_length = self.env_params.additional_params['ring_length'][1]

                observation = np.array([
                self.k.vehicle.get_speed(rl_id) / max_speed,
                (self.k.vehicle.get_speed(lead_id) -
                self.k.vehicle.get_speed(rl_id)) / max_speed,
                (self.k.vehicle.get_x_by_id(lead_id) -
                self.k.vehicle.get_x_by_id(rl_id)) % self.k.network.length()
                / max_length
                ])
            
            # Dont observe the leader
            else:
                observation = np.array([-1, -1, -1]) # the second -1 could be plausible above but unlikely

            # For lead RL append the TSE output (it already considers to be only in zone)
            if rl_id == self.lead_rl_id:
                observation = np.append(observation, self.tse_output_encoded)
                self.leader_obs = observation
            # For others    
            else:
                 observation = np.append(observation, np.zeros(6)) # Dummy, zeros (because observation space is fixed)
            
            obs.update({rl_id: observation})
            print(f"RL_ID: {rl_id.split('_')[1]}, observation: {observation}, shape: {observation.shape}")
        print("\n")
        
        #print(f"Observations new: {obs} \n")
        return obs



    def reset(self, new_inflow_rate=None):
        """See parent class.

        The sumo instance is reset with a new ring length, and a number of
        steps are performed with the rl vehicle acting as a human vehicle.
        """
        # skip if ring length is None
        if self.env_params.additional_params['ring_length'] is None:
            return super().reset()

        # reset the step counter
        self.step_counter = 0

        # update the network
        initial_config = InitialConfig(bunching=50, min_gap=0)
        length = random.randint(
            self.env_params.additional_params['ring_length'][0],
            self.env_params.additional_params['ring_length'][1])
        additional_net_params = {
            'length':
                length,
            'lanes':
                self.net_params.additional_params['lanes'],
            'speed_limit':
                self.net_params.additional_params['speed_limit'],
            'resolution':
                self.net_params.additional_params['resolution']
        }
        net_params = NetParams(additional_params=additional_net_params)

        self.network = self.network.__class__(
            self.network.orig_name, self.network.vehicles,
            net_params, initial_config)
        self.k.vehicle = deepcopy(self.initial_vehicles)
        self.k.vehicle.kernel_api = self.k.kernel_api
        self.k.vehicle.master_kernel = self.k

        # solve for the velocity upper bound of the ring
        v_guess = 4
        v_eq_max = fsolve(v_eq_max_function, np.array(v_guess),
                          args=(len(self.initial_ids), length))[0]

        print('\n-----------------------')
        print('ring length:', net_params.additional_params['length'])
        print('v_max:', v_eq_max)
        print('-----------------------')

        # restart the sumo instance
        self.restart_simulation(
            sim_params=self.sim_params,
            render=self.sim_params.render)

        # perform the generic reset function
        return super().reset()

    def additional_command(self):
        """Define which vehicles are observed for visualization purposes."""
        # specify observed vehicles
        current_length = self.k.network.length()
        vehicles_in_zone = self.k.vehicle.get_veh_list_local_zone(self.lead_rl_id, current_length, self.LOCAL_ZONE )
        for veh_id in vehicles_in_zone:
            self.k.vehicle.set_observed(veh_id)