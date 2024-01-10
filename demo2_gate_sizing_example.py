import faulthandler
faulthandler.enable()

import os
import torch
import torch.optim as optim
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import random
from collections import namedtuple
import dgl
import math
from tqdm import tqdm
from time import time
import json
import sys
import openroad as ord
from openroad import Tech, Design, Timing
import copy
from pathlib import Path


from demo2_gate_sizing_helpers import *

import argparse
###############
#path argumant#
###############
parser = argparse.ArgumentParser(description="path of your ASPDAC2024-Turotial clone (must include /ASPDAC2024-Turotial)")
parser.add_argument("--path", type = str, default='./', action = 'store')
pyargs = parser.parse_args()
###################
#set up matplotlib#
###################
is_ipython = 'inline' in matplotlib.get_backend()
if is_ipython:
  from IPython import display
##################################
#use gpu or cpu(cpu for tutorial)#
##################################
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
print(device)
####################################################
#load cell dictionary with name and size properties#
####################################################
with open(pyargs.path + '/data/cell_dictionary_openroad_modified.json','r') as f:
  cell_dict = json.load(f)
###################################################
#create a lookup table for the index form the name#
###################################################
cell_name_dict = {}
for k,v in cell_dict.items():
  cell_name_dict[v['name']] = k
########################################
#laod design using openroad python apis#
########################################
ord_tech, ord_design, timing, db, chip, block, nets = load_design(pyargs.path)
################################################################################
#srcs, dsts : source and destination instances for the graph function.         #
#inst_dict : Dictionary that stores all the properties of the instances.       #
#fanin_dict, fanout_dict : Dictionary that keeps a stores the fanin and        #
#                          fanout of the instances in an easily indexable way. #
#endpoints : Storing all the endpoints(here they are flipflops)                #
################################################################################
inst_dict, endpoints, srcs, dsts, fanin_dict, fanout_dict = \
iterate_nets_get_properties(ord_design, timing, nets, block, cell_dict, cell_name_dict)
################################################
#quick lookup for the instance name from the ID#
################################################
inst_names = {v['idx']:k for k,v in inst_dict.items()}
# create DGL graph
G = dgl.graph((srcs+dsts,dsts+srcs))
# store the featues for cell types, slack, slew, load, area, and max_size_index(for validity checks)
G.ndata['cell_types'] = torch.tensor([ inst_dict[x]['cell_type'] for x in inst_names.values() ])
G.ndata['slack'] = torch.tensor(
  [ inst_dict[x]['slack'] for x in inst_names.values() ])
G.ndata['slew'] = torch.tensor(
  [ inst_dict[x]['slew'] for x in inst_names.values() ])
G.ndata['load'] = torch.tensor(
  [ inst_dict[x]['load'] for x in inst_names.values() ])
G.ndata['area'] = torch.tensor([ inst_dict[x]['area'] for x in inst_names.values() ])
G.ndata['max_size'] = torch.tensor([cell_dict[str(inst_dict[x]['cell_type'][0])]['n_sizes'] for x in inst_names.values()])
G.edata['types'] = torch.cat((torch.zeros(len(srcs),dtype=torch.long),torch.ones(len(dsts),dtype=torch.long)),0)
# normalization parameters
norm_data = {
  'max_area' : 1.0*np.max(G.ndata['area'].numpy()),
  'clk_period' : CLKset[0],
  'max_slew' : 1.0*np.max(G.ndata['slew'].numpy()),
  'max_load' : 1.0*np.max(G.ndata['load'].numpy()),
}
print(norm_data)
G.ndata['area'] = G.ndata['area']/norm_data['max_area']
G.ndata['slack'] = G.ndata['slack']/norm_data['clk_period']
for i in range(len(G.ndata['slack'])):
  if G.ndata['slack'][i] > float(1):
    G.ndata['slack'][i] = 1
G.ndata['slack'][torch.isinf(G.ndata['slack'])] = 1
G.ndata['slew'] = G.ndata['slew']/norm_data['max_slew']
G.ndata['load'] = G.ndata['load']/norm_data['max_load']


inital_total_area = torch.sum(G.ndata['area'])*norm_data['max_area']/(unit_micron*unit_micron)
print("initial total area: %.4f"%inital_total_area)
print(device)
print("=========================================================")
G = G.to(device)

print("Number of Nodes:",G.num_nodes())
print("Number of Edges:",G.num_edges())
Transition = namedtuple('Transition',
                        ('graph', 'action', 'next_state', 'reward'))

n_cells = max([int(x) for x in cell_dict.keys()]) + 1
n_state= n_cells+n_features

###############################################################
#Give an intial solution proportionaly to the original slacks.#
###############################################################
Slack_Lambda = 1-G.ndata['slack'].to('cpu')
#############################################################################
#Create the target and policy nets and ensure that they have the same value.#
#############################################################################
policy_net = DQN(n_state, n_cells, n_features, device).to(device)
target_net = DQN(n_state, n_cells, n_features, device).to(device)
target_net.load_state_dict(policy_net.state_dict())
if retrain:
    target_net.load_state_dict(torch.load('models/%s/%s_target_local_lr_rgcn.ckpt'%(design,design)))
    policy_net.load_state_dict(torch.load('models/%s/%s_policy_local_lr_rgcn.ckpt'%(design,design)))
if inference:
    target_net.load_state_dict(torch.load('models/%s/%s_target_local_lr_rgcn.ckpt'%(design,design)))
    policy_net.load_state_dict(torch.load('models/%s/%s_policy_local_lr_rgcn.ckpt'%(design,design)))
target_net.eval()

optimizer = optim.RMSprop(policy_net.parameters(), lr = LR)
####################
#Experience Storage#
####################
memory = ReplayMemory(BUF_SIZE)
steps_done = 0

loss_history = []
episode_durations = []

episode_inst_dict = copy.deepcopy(inst_dict)
episode_G = copy.deepcopy(G)

print("Worst Negative slack initial:", torch.min(episode_G.ndata['slack'])*norm_data['clk_period'])
print("Total Negative slack initial:", torch.sum(torch.min(episode_G.ndata['slack'],torch.zeros_like(episode_G.ndata['slack']))))
episode_reward = []
update_loss = []
update_step = []

if inference:
  soln_space_fname = pyargs.path + "/data/solution_space_%s_%s_%s.txt"%(design,clk_final,semi_opt_clk)
  cell_size_dirname = pyargs.path + "/cell_sizes/%s_inf"%design
else:
  soln_space_fname = pyargs.path + "/data/solution_space_%s_%s_%s.txt"%(design,clk_final,semi_opt_clk)
  cell_size_dirname = pyargs.path + "/cell_sizes/%s"%design
cell_size_dir = Path(cell_size_dirname)

if not cell_size_dir.exists():
  cell_size_dir.mkdir(parents=True, exist_ok=True)

#create empty file at start
with open(soln_space_fname,"w") as f:
  pass

if not(os.path.exists(pyargs.path + "/models")):
  os.mkdir(pyargs.path + "/models")

if not(os.path.exists(pyargs.path + "/models/{}".format(design))):
  os.mkdir(pyargs.path + "/models/{}".format(design))

if not(os.path.exists(pyargs.path + "/logs")):
  os.mkdir(pyargs.path + "/logs")

epreward = open ("epreward.txt","w")
epreward.truncate()
epreward.close()
print("RUNNING")
train_start_time = time()
if inference:
  MAX_STEPS = 75
  min_slack_plot = torch.min(episode_G.ndata['slack']*norm_data['clk_period']).item()
if inference:
  num_episodes = 1
  tracking_d = open ("tracking_d.txt","w")
  tracking_d.truncate()
  tracking_d.close()
  tracking_a = open ("tracking_a.txt","w")
  tracking_a.truncate()
  tracking_a.close()
  tracking_inst = open ("tracking_inst.txt","w")
  tracking_inst.truncate()
  tracking_inst.close()
else:
  num_episodes = EPISODE

pareto_points = []
pareto_cells  = []
best_cost = calc_cost(episode_G, Slack_Lambda)
reset_state = get_state_cells(episode_inst_dict, inst_names, cell_dict)
data_v_episode = []
for i_episode in range(num_episodes):
  random_taken = 0
  total_taken = 0
  print("Episode :",i_episode)
  # Initialize the environment and state
  episode_G, episode_inst_dict, working_clk = env_reset(reset_state, i_episode,\
      cell_name_dict, CLKset, ord_design, timing, G, inst_dict, CLK_DECAY, CLK_DECAY_STRT,\
      clk_init, clk_range, clk_final, inst_names, block, cell_dict, norm_data, device)
  best_cost = calc_cost(episode_G, Slack_Lambda)
  print("RESET")
  print("RESET COST:",best_cost)
  cumulative_reward = 0
  count_bads = 0
  
  old_WNS = torch.min(episode_G.ndata['slack'])
  old_TNS = torch.sum(torch.min(episode_G.ndata['slack'], torch.zeros_like(episode_G.ndata['slack'])))
  old_area = torch.sum(episode_G.ndata['area'])
  episode_TNS = old_TNS
  episode_WNS = old_WNS
  episode_area = old_area
  print("episode TNS",episode_TNS*norm_data['clk_period'])
  for t in range(MAX_STEPS):
    print("################STEP {:2d}################".format(t))
    #while True:
    ## Select and perform an action
    st =time()
    critical_nodes = get_critical_path_nodes(episode_G, i_episode, TOP_N_NODES, n_cells)
    critical_graph = get_subgraph(episode_G, critical_nodes)
    state  = get_state(critical_graph, n_state, n_cells, n_features)
    action, total_taken, steps_done, random_taken\
                        = select_action(critical_graph, inference, total_taken,\
                          steps_done, random_taken, policy_net,\
                          EPS_END, EPS_START, EPS_DECAY, device)
    st = time()
    if action == -1:
      print("no action")
      cost = calc_cost(episode_G, Slack_Lambda)
      if cost<best_cost:
        best_cost = cost
        reset_state = get_state_cells(episode_inst_dict, inst_names, cell_dict)
      break
    reward, done_env, next_state, episode_inst_dict, episode_G  = env_step(episode_G, critical_graph,\
        state, action.item(), CLKset, ord_design, timing, cell_dict, norm_data, inst_names,\
        episode_inst_dict, inst_dict, n_cells, n_features, block, device, Slack_Lambda, eps)
    reward = torch.tensor([reward], device=device, dtype=torch.float32)
    new_area = torch.sum(episode_G.ndata['area'])
    if inference:
      print("cell: ", inst_names[int(action//2)])
      print("action: ", 'Downsize' if(action%2) else 'Upsize' )
      if done_env:
        done = 1
      else:
        done = 0
    else:
      done = 0
    solution = []
    sizes = []
    for i in range(len(episode_G.ndata['cell_types'][:,1].T)):
      if episode_G.ndata['cell_types'][:,1].T[i] > 0:
        solution.append(i)
        sizes.append(int(episode_G.ndata['cell_types'][:,1].T[i]))
    if t >= MAX_STEPS:
      done = 1

    if reward < 0:
      count_bads += 1
    else:
      count_bads = 0
    if count_bads >= STOP_BADS:
      print("Stopping bad actions")
      count_bads = 0
      done = 1

    cumulative_reward += reward
    if done:
      next_state = None
      next_state_push =None
    else:
      next_state_push = next_state

    if not inference:
      memory.push(critical_graph.clone(), action,next_state_push, reward)
      # Perform one step of the optimization (on the target network)
      optimizer, loss_history = optimize_model(memory, BATCH_SIZE, device, GAMMA,\
                                              policy_net, target_net, optimizer, loss_history)

    if next_state != None:
      new_slacks = np.array([x['slack'] for x in episode_inst_dict.values()])/norm_data['clk_period']
      new_slacks[np.isinf(new_slacks)] = 1
      for i in range(len(new_slacks)):
        if new_slacks[i] > float(1):
          new_slacks[i] = 1  
      new_slacks = np.minimum(new_slacks,np.zeros_like(new_slacks))
      new_WNS = np.min(new_slacks)
      new_TNS = np.sum(new_slacks)
      if new_WNS >max_WNS:
        
        print("################################ NEW WNS ################################")
        print(new_WNS.item()*norm_data['clk_period'])
        max_WNS = new_WNS

      if new_TNS >max_TNS:
        print("################################ NEW TNS ################################")
        print(new_TNS.item()*norm_data['clk_period'])
        max_TNS = new_TNS
      working_clk_period = (working_clk - new_WNS*norm_data['clk_period']).item()
      if working_clk_period < min_working_clk:
        print("############################ operating delay ############################")
        print(working_clk_period)
        min_working_clk = working_clk_period
        print("#########################################################################")
      working_area = new_area*norm_data['max_area']/(unit_micron*unit_micron)
      point_time = time()
      with open(soln_space_fname,"a") as f:
        f.write("%7.4e, %7.4e, %d\n"%(working_clk_period, working_area, point_time-train_start_time))
      ret = pareto(pareto_points, pareto_cells, float(working_area),\
                    working_clk_period, episode_inst_dict, inst_names,\
                    cell_dict, inst_dict, block, ord_design, timing)
      if(ret == 1):
        print("pareto point")
        print("%7.4e, %7.4e\n"%(working_clk_period, working_area))
        l= len(pareto_points)
        print(action)
        slacks = [min_slack(block.findITerm(x + cell_dict[str(inst_dict[x]['cell_type'][0])]['out_pin']), timing) for x in inst_names.values()]
      cost = calc_cost(episode_G, Slack_Lambda)
      if cost<best_cost:
        best_cost = cost
        reset_state = get_state_cells(episode_inst_dict, inst_names, cell_dict)
      if new_TNS > episode_TNS:
        episode_TNS = new_TNS
      if new_WNS> episode_WNS:
        episode_WNS = new_WNS
      if new_area < episode_area:
        episode_area = new_area
    else:
      new_WNS = torch.FloatTensor([0])
      new_TNS = torch.FloatTensor([0])

    working_clk_period = (working_clk-(new_WNS)*norm_data['clk_period']).item()
    if t%10 == 9 :
      Slack_Lambda = update_lambda(Slack_Lambda, episode_G.ndata['slack'].to('cpu'), K)
    if t%25 == 24 :
      print("Taken", random_taken/total_taken)
      print("old_WNS: {}   new_WNS: {}".format(old_WNS.item(), new_WNS.item()))
      print("Updated WNS",old_WNS.item()*norm_data['clk_period'], new_WNS.item()*norm_data['clk_period'])
      print("Updated TNS",old_TNS.item()*norm_data['clk_period'], new_TNS.item()*norm_data['clk_period'])
      print(" # Critical Nodes", critical_nodes.size(), critical_graph.num_nodes())
    st = time()

    old_WNS = new_WNS
    old_TNS = new_TNS

    if (inference and  (working_clk_period < best_delay)):
      best_delay = working_clk_period
      tracking_d = open ("tracking_d.txt","a")
      tracking_a = open ("tracking_a.txt","a")
      tracking_d.write(str(best_delay)+',')
      tracking_a.write(str(float('%.4f' % new_area))+',')
      tracking_d.close()
      tracking_a.close()
      print(best_delay,new_area)
      solution = []
      sizes = []
      for i in range(len(episode_G.ndata['cell_types'][:,1].T)):
        if episode_G.ndata['cell_types'][:,1].T[i] > 0:
          solution.append(i)
          sizes.append(int(episode_G.ndata['cell_types'][:,1].T[i]))
      print(solution)
      print(sum(episode_G.ndata['cell_types'][:,1].T))
      print(sizes)
      tracking_inst = open ("tracking_inst.txt","a")
      #tracking_inst.write(str(int(action))+',')
      tracking_inst.write("size_cell "+inst_names[int(action//2)]+" "
                      +cell_dict[str(episode_inst_dict[inst_names[int(action//2)]]['cell_type'][0])]['name']
                      +cell_dict[str(episode_inst_dict[inst_names[int(action//2)]]['cell_type'][0])]['sizes'][int(episode_inst_dict[inst_names[int(action//2)]]['cell_type'][1])]
                      +'\n')
      tracking_inst.close()

    # Move to the next state
    if done:
      episode_durations.append(t + 1)
      break
    if (len(loss_history)+1) % TARGET_UPDATE == 0:#(i_episode) % TARGET_UPDATE
      if (i_episode < UPDATE_STOP):
        target_net.load_state_dict(policy_net.state_dict())
        print("Target updated")
        if len(loss_history)>0:
          update_loss.append(loss_history[-1])
          update_step.append(len(loss_history)-1)
  print("LEN(PARETO_POINTS)",len(pareto_points))
  data_v_episode.append((float(episode_TNS*norm_data['clk_period']),float(episode_WNS*norm_data['clk_period']), float(episode_area*norm_data['max_area'] )))
  if i_episode % 50 == 0:
    print(time())
    training_log = open (pyargs.path + "/logs/trainLog_%s.txt"%design,"a")
    training_log.write('\n'+str(time())+'\n')
    training_log.close()
  episode_reward.append(cumulative_reward.item())
  epreward = open (pyargs.path + "/logs/epreward_%s.txt"%design,"a")
  epreward.write(str(cumulative_reward.item())+',')
  epreward.close()
  torch.save(policy_net.state_dict(), pyargs.path + '/models/%s/trained_ep_p_local_lr_RGCN_%s.ckpt'%(design,design))
  torch.save(target_net.state_dict(), pyargs.path + '/models/%s/trained_ep_t_local_lr_RGCN_%s.ckpt'%(design,design))
  critical_nodes = get_critical_path_nodes(episode_G, i_episode, TOP_N_NODES, n_cells)
  print("Episode End critical nodes",critical_nodes, critical_nodes.size())

print(time())
training_log = open (pyargs.path + "/logs/trainLog_%s.txt"%design,"a")
training_log.write('\n'+str(time())+'\n')
training_log.close()
print('Complete')

sorted_pareto_points = np.array(sorted(pareto_points, key=lambda x: x[0]))
pareto_fname = pyargs.path + "/data/pareto_points_%s_%s_%s.txt"%(design,clk_final,semi_opt_clk)
pf = open(pareto_fname, 'w')
for i in range(len(sorted_pareto_points[:,0])):
  pf.write("%7.4e, %7.4e\n"%(sorted_pareto_points[i,1],sorted_pareto_points[i,0]))
pf.close()
for n, point in enumerate(pareto_points):
  point_file = cell_size_dir/("clk_%d_ps_%s_%s_%s.txt"%(point[1]*1000,design,clk_final,semi_opt_clk))
  with point_file.open("w") as f:
    f.write("# Design: %s\n"%design)
    f.write("# Target clk: %5.4f ns\n"%(CLKset[0]))
    f.write("# Area: %5.4f\n"%(point[0]))
    f.write("# Working clock: %5.4f ns\n"%(point[1]))
    for i, inst_name in enumerate(inst_names.values()):
      f.write("size_cell %s %s\n"%(inst_name,pareto_cells[n][i]))

data_v_episode = np.array(data_v_episode)
TNS_fname = pyargs.path + "/data/TNS_converging_plot_%s_%s_%s.txt"%(design,clk_final,semi_opt_clk)
WNS_fname = pyargs.path + "/data/WNS_converging_plot_%s_%s_%s.txt"%(design,clk_final,semi_opt_clk)
area_fname = pyargs.path + "/data/area_converging_plot_%s_%s_%s.txt"%(design,clk_final,semi_opt_clk)

np.savetxt(TNS_fname, data_v_episode[:,0], fmt='%5.4f')
np.savetxt(WNS_fname, data_v_episode[:,1], fmt='%5.4f')
np.savetxt(area_fname, data_v_episode[:,2], fmt='%5.4f')

G.num_nodes()
print(max(episode_reward))

if inference == False:
  torch.save(policy_net.state_dict(), pyargs.path + '/models/%s/%s_policy_local_lr_rgcn.ckpt'%(design,design))
  torch.save(target_net.state_dict(), pyargs.path + '/models/%s/%s_target_local_lr_rgcn.ckpt'%(design,design))

print("#################Done#################")
