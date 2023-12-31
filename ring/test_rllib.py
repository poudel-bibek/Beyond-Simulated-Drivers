"""
This is modified code from the visualizer_rllib.py file.
May contain some code that is not used.

Should be able to specify the ring length
Should be able to set shock vehicles 
Should be able to specify the warmup, horizon, shock time

Should be able to calculate shock times according to shock model and accelerate accordingly

If a shock start time is set to 8000, this program excludes the warmuptime and starts counting timesteps after warmup end
So the clock will display 8000+ warmup as shock start time.
So just set an offset that corrects this behavior to make it consistent across classic controllers and RL
Rollout files does contain behavior or RL before warmup 
"""

import argparse
import gym
import numpy as np
import os
import sys
import time

import ray
try:
    from ray.rllib.agents.agent import get_agent_class
except ImportError:
    from ray.rllib.agents.registry import get_agent_class
from ray.tune.registry import register_env

from flow.core.util import emission_to_csv
from flow.utils.registry import make_create_env
from flow.utils.rllib import get_flow_params
from flow.utils.rllib import get_rllib_config
from flow.utils.rllib import get_rllib_pkl

import json 
from common_args import update_arguments
from flow.density_aware_util import get_shock_model, get_time_steps, get_time_steps_stability
import random 


EXAMPLE_USAGE = """
example usage:
    python ./test_rllib.py /ray_results/experiment_dir/result_dir 1

Here the arguments are:
1 - the path to the simulation results
2 - the number of the checkpoint
"""



def visualizer_rllib(args):
    
    result_dir = args.result_dir if args.result_dir[-1] != '/' \
        else args.result_dir[:-1]

    #print("\n1.:",result_dir)
    config = get_rllib_config(result_dir)
    #print(config)

    #First figure out if the environment is multiagent or not
    if config.get('multiagent', {}).get('policies', None):
        multiagent = True
        pkl = get_rllib_pkl(result_dir)
        config['multiagent'] = pkl['multiagent']
        # print(f"\n\nmultiagent{config['multiagent']}\n\n")
    else:
        multiagent = False

    # Modify the config here, its in the dict form. below they are instantiated
    flow_params_modify = json.loads(config["env_config"]["flow_params"])

    # 1. Be able to specify ring length
    if args.length is not None:
        flow_params_modify["env"]["additional_params"]["ring_length"] = [args.length,args.length]

    #print(f"\n\nflow_params_modify= {flow_params_modify}\n\n")

    if multiagent:
        # For some reason, in multiagent, RL vehicles are veh[0]..onwards
        human_index = NUM_AUTOMATED # This is the index within "veh"

        flow_params_modify["veh"][human_index]["acceleration_controller"] = ["ModifiedIDMController", {"noise": args.noise,
                                                                                            "shock_vehicle": True}]
        
        flow_params_modify["veh"][human_index]["car_following_params"]["controller_params"]["minGap"] = args.min_gap

        # The leader RL has to be made RL controller, its the last RL
        leader_index = NUM_AUTOMATED - 1
        flow_params_modify["veh"][leader_index]["acceleration_controller"] = ["RLController", {}]
        flow_params_modify["veh"][leader_index]["car_following_params"]["controller_params"] = {
            "accel": 2.6, 
            "carFollowModel": "IDM", 
            "decel": 4.5, 
            "impatience": 0.5, 
            "maxSpeed": 30, 
            "minGap": 0.1, 
            "sigma": 0.5, 
            "speedDev": 0.1, 
            "speedFactor": 1.0, 
            "tau": 1.0
        }
        flow_params_modify["veh"][leader_index]["car_following_params"]["speed_mode"] = 25
        flow_params_modify["veh"][leader_index]["initial_speed"] = 0
        flow_params_modify["veh"][leader_index]["lane_change_controller"] = ["SimLaneChangeController", {}]
        flow_params_modify["veh"][leader_index]["lane_change_params"] = {
            "controller_params": {
                "laneChangeModel": "LC2013", 
                "lcCooperative": "1.0", 
                "lcKeepRight": "1.0", 
                "lcSpeedGain": "1.0", 
                "lcStrategic": "1.0"
            }, 
            "lane_change_mode": 512
        }
        flow_params_modify["veh"][leader_index]["num_vehicles"] = 1
        flow_params_modify["veh"][leader_index]["routing_controller"] = ["ContinuousRouter", {}]
        #flow_params_modify["veh"][leader_index]["veh_id"] = "rl_3" # This is already set

    else: 
        # 2. Be able to set shock vehicles (set the IDM vehicles to ModifiedIDM)
        # "veh" is a list with the first element as humans. Right now all humans are shock vehicles, modify for stability tests
        flow_params_modify["veh"][0]["acceleration_controller"] = ["ModifiedIDMController", {"noise": args.noise, # Just need to specify as string
                                                                                            "shock_vehicle": True}] 
        # 2.1 modify the mingap for human vehicles (only at test time)
        flow_params_modify["veh"][0]["car_following_params"]["controller_params"]["minGap"] = args.min_gap

    # 3. Be able to specify warmup, horizon, shock time
    flow_params_modify["env"]["horizon"] = args.horizon
    flow_params_modify["env"]["warmup_steps"] = args.warmup

    # shock params argument does not exist, so we need to add it
    #flow_params_modify["env"]["additional_params"]["shock_params"] = {"shock_time": args.shock_time, "shock_duration": args.shock_duration}

    config["env_config"]["flow_params"] = json.dumps(flow_params_modify)
    #print(f"\n\nconfig['env_config']['flow_params'] = {config['env_config']['flow_params']}\n\n")
    # Run on only one cpu for rendering purposes
    config['num_workers'] = 0

    flow_params = get_flow_params(config)

    # hack for old pkl files
    # TODO(ev) remove eventually
    sim_params = flow_params['sim']
    setattr(sim_params, 'num_clients', 1)

    # for hacks for old pkl files TODO: remove eventually
    if not hasattr(sim_params, 'use_ballistic'):
        sim_params.use_ballistic = False

    # Determine agent and checkpoint
    config_run = config['env_config']['run'] if 'run' in config['env_config'] \
        else None
    if args.run and config_run:
        if args.run != config_run:
            print('visualizer_rllib.py: error: run argument '
                  + '\'{}\' passed in '.format(args.run)
                  + 'differs from the one stored in params.json '
                  + '\'{}\''.format(config_run))
            sys.exit(1)
    if args.run:
        agent_cls = get_agent_class(args.run)
    elif config_run:
        agent_cls = get_agent_class(config_run)
    else:
        print('visualizer_rllib.py: error: could not find flow parameter '
              '\'run\' in params.json, '
              'add argument --run to provide the algorithm or model used '
              'to train the results\n e.g. '
              'python ./visualizer_rllib.py /tmp/ray/result_dir 1 --run PPO')
        sys.exit(1)

    sim_params.restart_instance = True

    dir_path = os.path.dirname(os.path.realpath(__file__))
    rl_folder_name = f"{args.method}_stability" if args.stability else args.method
    emission_path = f"{dir_path}/test_time_rollout/{rl_folder_name}" #'{0}/test_time_rollout/'.format(dir_path)

    sim_params.emission_path = emission_path if args.gen_emission else None

    # Create and register a gym+rllib env
    create_env, env_name = make_create_env(params=flow_params, version=0)
    register_env(env_name, create_env)

    # Start the environment with the gui turned on and a path for the
    # emission file
    env_params = flow_params['env']
    env_params.restart_instance = False
    if args.evaluate:
        env_params.evaluate = True

    # lower the horizon if testing
    if args.horizon:
        config['horizon'] = args.horizon
        env_params.horizon = args.horizon

    # create the agent that will be used to compute the actions
    agent = agent_cls(env=env_name, config=config)
    checkpoint = result_dir + '/checkpoint_' + args.checkpoint_num
    checkpoint = checkpoint + '/checkpoint-' + args.checkpoint_num
    #print("\n\n2.:",checkpoint,"\n\n")
    agent.restore(checkpoint)

    if hasattr(agent, "local_evaluator") and \
            os.environ.get("TEST_FLAG") != 'True':
        env = agent.local_evaluator.env
    else:
        env = gym.make(env_name)

    # if args.render_mode == 'sumo_gui':
    #     env.sim_params.render = True  # set to True after initializing agent and env
    if args.render: 
        env.sim_params.render = True
    else: 
        env.sim_params.render = False

    if multiagent:
        rets = {}
        # map the agent id to its policy
        policy_map_fn = policy_mapping_fn #config['multiagent']['policy_mapping_fn']

        # Bibek: Debug hack. define the policy mapping function here instead of getting it form file

        for key in config['multiagent']['policies'].keys():
            rets[key] = []
    else:
        rets = []

    if config['model']['use_lstm']:
        use_lstm = True
        if multiagent:
            state_init = {}
            # map the agent id to its policy
            policy_map_fn = config['multiagent']['policy_mapping_fn']
            size = config['model']['lstm_cell_size']
            for key in config['multiagent']['policies'].keys():
                state_init[key] = [np.zeros(size, np.float32),
                                   np.zeros(size, np.float32)]
        else:
            state_init = [
                np.zeros(config['model']['lstm_cell_size'], np.float32),
                np.zeros(config['model']['lstm_cell_size'], np.float32)
            ]
    else:
        use_lstm = False

    # if restart_instance, don't restart here because env.reset will restart later
    if not sim_params.restart_instance:
        env.restart_simulation(sim_params=sim_params, render=sim_params.render)

    # Simulate and collect metrics
    final_outflows = []
    final_inflows = []
    mean_speed = []
    std_speed = []

    warmup_offset = args.warmup 
    shock_start_time = args.shock_start_time - warmup_offset
    shock_end_time = args.shock_end_time - warmup_offset

    for i in range(args.num_rollouts):
        vel = []
        state = env.reset()
        if multiagent:
            ret = {key: [0] for key in rets.keys()}
        else:
            ret = 0

        shock_model_id = -1 if args.stability else args.shock_model
        intensities, durations, frequency =  get_shock_model(shock_model_id, length = args.length) if args.stability else get_shock_model(shock_model_id)
        if args.stability:
            shock_times = get_time_steps_stability(durations, frequency, shock_start_time, shock_end_time)
        else:
            shock_times = get_time_steps(durations, frequency, shock_start_time, shock_end_time)

        shock_counter = 0
        current_duration_counter = 0
        vehicles = env.unwrapped.k.vehicle
        single_shock_id = random.choice([item for item in vehicles.get_ids() if item not in vehicles.get_rl_ids()])

        # This program counts for warmup or not? 
        # This will start running only after warmup ends 
        for step in range(env_params.horizon):
            
            speeds = vehicles.get_speed(vehicles.get_ids())

             # TODO: update for stability
            # perform_shock function RL version
            if args.shock and step >= shock_start_time and step <= shock_end_time:
                if args.stability:
                    single_shock_id, shock_counter, current_duration_counter = perform_shock_stability(env, shock_times, shock_counter, current_duration_counter, step, intensities, durations, frequency, num_automated = NUM_AUTOMATED) # For stability the values are single values
                else:
                    single_shock_id, shock_counter, current_duration_counter = perform_shock(env, vehicles, single_shock_id, \
                        shock_times, shock_counter, current_duration_counter, step, intensities, durations, frequency)

                

            # only include non-empty speeds
            if speeds:
                vel.append(np.mean(speeds))

            if multiagent:
                action = {}
                for agent_id in state.keys():
                    if use_lstm:
                        action[agent_id], state_init[agent_id], logits = \
                            agent.compute_action(
                            state[agent_id], state=state_init[agent_id],
                            policy_id=policy_map_fn(agent_id))
                    else:
                        action[agent_id] = agent.compute_action(
                            state[agent_id], policy_id=policy_map_fn(agent_id))
            else:
                action = agent.compute_action(state)
            state, reward, done, _ = env.step(action)
            if multiagent:
                for actor, rew in reward.items():
                    ret[policy_map_fn(actor)][0] += rew
            else:
                ret += reward
            if multiagent and done['__all__']:
                break
            if not multiagent and done:
                break

        if multiagent:
            for key in rets.keys():
                rets[key].append(ret[key])
        else:
            rets.append(ret)
        outflow = vehicles.get_outflow_rate(500)
        final_outflows.append(outflow)
        inflow = vehicles.get_inflow_rate(500)
        final_inflows.append(inflow)
        if np.all(np.array(final_inflows) > 1e-5):
            throughput_efficiency = [x / y for x, y in
                                     zip(final_outflows, final_inflows)]
        else:
            throughput_efficiency = [0] * len(final_inflows)
        mean_speed.append(np.mean(vel))
        std_speed.append(np.std(vel))
        if multiagent:
            for agent_id, rew in rets.items():
                print('Round {}, Return: {} for agent {}'.format(
                    i, ret, agent_id))
        else:
            print('Round {}, Return: {}'.format(i, ret))

    print('==== END ====')
    # print("Return:")
    # print(mean_speed)
    # if multiagent:
    #     for agent_id, rew in rets.items():
    #         print('For agent', agent_id)
    #         print(rew)
    #         print('Average, std return: {}, {} for agent {}'.format(
    #             np.mean(rew), np.std(rew), agent_id))
    # else:
    #     print(rets)
    #     print('Average, std: {}, {}'.format(
    #         np.mean(rets), np.std(rets)))

    # print("\nSpeed, mean (m/s):")
    # print(mean_speed)
    # print('Average, std: {}, {}'.format(np.mean(mean_speed), np.std(
    #     mean_speed)))
    # print("\nSpeed, std (m/s):")
    # print(std_speed)
    # print('Average, std: {}, {}'.format(np.mean(std_speed), np.std(
    #     std_speed)))

    # # Compute arrival rate of vehicles in the last 500 sec of the run
    # print("\nOutflows (veh/hr):")
    # print(final_outflows)
    # print('Average, std: {}, {}'.format(np.mean(final_outflows),
    #                                     np.std(final_outflows)))
    # # Compute departure rate of vehicles in the last 500 sec of the run
    # print("Inflows (veh/hr):")
    # print(final_inflows)
    # print('Average, std: {}, {}'.format(np.mean(final_inflows),
    #                                     np.std(final_inflows)))
    # # Compute throughput efficiency in the last 500 sec of the
    # print("Throughput efficiency (veh/hr):")
    # print(throughput_efficiency)
    # print('Average, std: {}, {}'.format(np.mean(throughput_efficiency),
    #                                     np.std(throughput_efficiency)))

    # terminate the environment
    env.unwrapped.terminate()

def perform_shock_stability(env, shock_times, shock_counter, current_duration_counter, step, intensity, duration, frequency, num_automated):

    if num_automated == 4:
        # 3_0 for ours4x
        single_shock_id = 'human_3_0'
        reference_speed_limit_id = 'human_3_1'
    elif num_automated == 9:
        # 8_0 for ours9x
        single_shock_id = 'human_8_0'
        reference_speed_limit_id = 'human_8_1'
    else: 
        single_shock_id = 'human_0'
        reference_speed_limit_id = 'human_1'

    speed_limit = env.unwrapped.k.vehicle.get_max_speed(reference_speed_limit_id)

    # Be default, shock is not applied
    env.unwrapped.k.vehicle.set_max_speed(single_shock_id, speed_limit)

    if current_duration_counter == duration*10:
        shock_counter += 1
        current_duration_counter = 0
        
    if shock_counter< frequency:
        if step >= shock_times[shock_counter][0] and step <= shock_times[shock_counter][1]:
            print(f"Step = {step}, Shock params: {intensity}, {duration}, {frequency} applied to vehicle {single_shock_id}\n")
            env.unwrapped.k.vehicle.set_max_speed(single_shock_id, intensity)
            current_duration_counter+=1

    return single_shock_id, shock_counter, current_duration_counter
    
def perform_shock(env, vehicles, single_shock_id, shock_times, shock_counter, current_duration_counter, step, intensities, durations, frequency):

    controller = env.unwrapped.k.vehicle.get_acc_controller(single_shock_id)
    print(f"\n\nController: {controller}\n\n")
    # Default: at times when shock is not applied, get acceleration from IDM
    controller.set_shock_time(False)

    if current_duration_counter == durations[shock_counter]*10:
        shock_counter += 1
        current_duration_counter = 0
        single_shock_id = random.choice([item for item in vehicles.get_ids() if item not in vehicles.get_rl_ids()])
        
    if shock_counter< frequency:
        if step >= shock_times[shock_counter][0] and step <= shock_times[shock_counter][1]:
            
            print(f"Step = {step}, Shock params: {intensities[shock_counter]}, {durations[shock_counter]}, {frequency} applied to vehicle {single_shock_id}\n")
            
            controller.set_shock_time(True) 
            controller.set_shock_accel(intensities[shock_counter])

            current_duration_counter+= 1
    return single_shock_id, shock_counter, current_duration_counter

def create_parser():
    """Create the parser to capture CLI arguments."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='[Flow] Evaluates a reinforcement learning agent '
                    'given a checkpoint.',
        epilog=EXAMPLE_USAGE)

    # required input parameters
    parser.add_argument(
        'result_dir', type=str, help='Directory containing results')
    parser.add_argument('checkpoint_num', type=str, help='Checkpoint number.')

    # optional input parameters
    parser.add_argument(
        '--run',
        type=str,
        help='The algorithm or model to train. This may refer to '
             'the name of a built-on algorithm (e.g. RLLib\'s DQN '
             'or PPO), or a user-defined trainable function or '
             'class registered in the tune registry. '
             'Required for results trained with flow-0.2.0 and before.')
    
    parser.add_argument(
        '--evaluate',
        action='store_true',
        help='Specifies whether to use the \'evaluate\' reward '
             'for the environment.')

    # parser.add_argument(
    #     '--save_render',
    #     action='store_true',
    #     help='Saves a rendered video to a file. NOTE: Overrides render_mode '
    #          'with pyglet rendering.')

    #parser.add_argument('--render_mode',type=str,default='sumo_gui', help='Pick the render mode. Options include sumo_web3d, rgbd and sumo_gui')
    
    parser.add_argument('--method',type=str,default=None, help='Method name, can be [wu, ours]')
    return parser


if __name__ == '__main__':

    parser = create_parser()
    parser = update_arguments(parser)
    args = parser.parse_args()

    if args.method is None:
        raise ValueError("Method name must be specified, can be [wu, ours, ours4x, ours9x]")
    
    if args.method == "ours4x":
        # Bibek: Hack
        NUM_AUTOMATED = 4 # Change this accordingly for 4x and 9x

    elif args.method == "ours9x":
        NUM_AUTOMATED = 9

    else: 
        NUM_AUTOMATED = 1

    def policy_mapping_fn(agent_id):
        """
        map policy to agent, only required for multi-agents
        The naming convention has been changed slightly here
        Now its rl_3_0.. and such instead of rl_0_3.. and such
        """
        # Based on the assumption (which is correct for now). The last item is the leader
        leader_id = f"rl_{NUM_AUTOMATED-1}_0"
        if agent_id == leader_id:
            return 'leader'
        else:
            return 'follower'
            
    ray.init(num_cpus=1)
    visualizer_rllib(args)
