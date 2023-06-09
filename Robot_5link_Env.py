import numpy as np
from numba import njit,float64, int32, jit
from numba.typed import Dict
from numba.core import types
from env_config import dt, t_limit, thres, vel_thres, prox_thres, min_prox, vel_prox, tau_max, damping, P, D, jnt_vel_max, j_max
from optimized_functions import calc_jnt_err, PDControl, angle_calc
from optimized_functions_5L import nxt_state, proximity
from support_classes import vertex
from Object_v2 import rand_object
from Robot_5link import forward, reverse, S, l, a, shift, get_coords
from spare_tnsr_replay_buffer import ReplayBuffer

rng = np.random.default_rng()

# @njit((types.Tuple(float64[:],float64[:],float64,int32,types.boolean))
#       (float64[:],float64[:],float64,float64[:],Dict))
@njit(nogil=True)
def env_replay(th, w, t_start, th_goal, obs_dict, steps, S, a, l):
    '''
    inputs:
        th: [th1,th2,th3....]
        w: [w1, w2, ...]
        t_start: time at the start
        th_goal: target joint position to reach
        obs_dict: dictionary containing obs positions
        steps: int num of time steps to simulate
    returns: 
        th: reached th
        w: reached w
        score: the reward of the connections
        t: time step at termination
        flag: collision flag, True if no collsions
    '''
    t = t_start
    jnt_err = calc_jnt_err(th, th_goal)
    dedt  = -1 * w

    score = 0
    flag = True
    done = False
    if t>= t_limit/dt:
        done = True
        flag = False 
        score = -np.inf

    while not done and t<t_start+steps and t < t_limit/dt:
        tau = PDControl(jnt_err, dedt)
        obj_arr = obs_dict[t]
        temp = nxt_state(obj_arr, th, w, tau, a, l, S)
        nxt_th = temp[0:5,0]
        prox = temp[5,0]
        nxt_w = temp[0:5,1]

        t+=1
        jnt_err = calc_jnt_err(nxt_th, th_goal)
        dedt  = -1*w
        th = nxt_th
        w = nxt_w

        if t*dt >= t_limit:
            # print('terminated on t_limit')
            done = True
        elif np.all(np.abs(jnt_err) < np.array([0.03,.03,.03,.03,.03])): # no termination for vel
            # print('terminated by reaching goal')
            done = True
        elif prox < min_prox:
            # print('terminated by collison')
            # flag = False
            done = True

        score += -1 

    return th, w, score, t, flag

def gen_rand_pos(quad, S, l):
    # r = the total reach of the robot
    r = np.sum(S[1:-1]) + np.sum(l)
    xy = rng.random(3)
    if quad==2 or quad==3:
        xy[0] = -1*xy[0]
    if quad==3 or quad==4:
        xy[1] = -1*xy[1]
    
    mag = (r/2)*.9*rng.random() + r/2
    p = mag*xy/np.linalg.norm(xy) + np.array([0,0,.3])
    if p[2] < 0.05:
        p[2] = .05
    if p[2] > r*.7+.3:
        p[2] = r*.7+.3

    # orientation
    u = rng.random(3)
    u = u/np.linalg.norm(u)

    return p, u

def gen_obs_pos(obj_list):
    '''
    obj_list: list of environment objects
    returns: a dictionary whose keys are time steps and items are a
             3xn array with column vectors of the n object locations
    '''
    time_steps = int(np.ceil(t_limit/dt))
    t = 0
    # obs_dict = Dict.empty(key_type=types.int32, 
    #                       value_type=types.float64[:,:])
    obs_dict = Dict.empty(
        key_type=types.int64,
        value_type=types.float64[:,:]
    )
    temp = np.ones((3,len(obj_list)))
    while t < time_steps:
        i = 0
        for o in obj_list:
            center = o.curr_pos
            temp[:,i] = center
            o.step()
            i+=1
        obs_dict[t] = temp.copy()
        t+=1
    
    return obs_dict

# class action_space():
#     def __init__(self):
#         self.shape = np.array([5]) # three joint angles adjustments
#         self.high = np.ones(5) * tau_max
#         self.low = np.ones(5) * -tau_max

# class observation_space():
#     def __init__(self):
#         self.shape = np.array([5])  

class RobotEnv(object):
    def __init__(self, has_objects=True, num_obj=3, start=None, goal=None, name='robot_5L',batch_size=128):

        if isinstance(start, np.ndarray):
            self.start = start
            self.goal = goal
        else:
            q1 = rng.choice(np.array([1,2,3,4]))
            q2 = rng.choice(np.array([1,2,3,4]))
            while q1 == q2:
                q2 = rng.choice(np.array([1,2,3,4]))
            
            s, u = gen_rand_pos(q1, S, l)
            g, v = gen_rand_pos(q2, S, l)

            sol_found = False
            th1 = reverse(s,u,S,a,l)
            th1 = th1[0,:]
            while not sol_found:
                if np.any(np.isnan(th1)):
                    s,u = gen_rand_pos(q1,S,l)
                    th1 = reverse(s,u,S,a,l)
                    th1 = th1[0,:]
                else:
                    sol_found = True

            sol_found = False
            th2 = reverse(g,v,S,a,l)
            th2 = th2[0,:]
            while not sol_found:
                if np.any(np.isnan(th2)):
                    g,v = gen_rand_pos(q2,S,l)
                    th2 = reverse(g,v,S,a,l)
                    th2 = th2[0,:]
                else:
                    sol_found = True
        
            self.start = th1
            self.goal = th2

        self.th = self.start
        self.w = np.zeros_like(self.start, dtype=float)
        self.t_step = 0
        self.t_sum = 0
        self.done = False
        max_size = int(np.ceil(t_limit/dt))
        self.memory = ReplayBuffer(max_size,jnt_d=self.th.shape[0], time_d=6, file=name)
        self.batch_size = batch_size
        self.info = {}
        self.jnt_err = calc_jnt_err(self.th, self.goal)
        self.dedt = np.zeros_like(self.jnt_err, dtype=float)

        if has_objects:
            objs = []
            i = 0
            k = 0
            while i < (num_obj) and k < int(1e3):
                k+=1
                o = rand_object(dt=dt)
                prox = np.inf 
                for j in range(30):
                    pos_j = o.path(j)
                    prox_i = proximity(pos_j, self.start, a, l,S)
                    if prox_i < prox:
                        prox = prox_i
                if prox > min_prox:
                    objs.append(rand_object(dt=dt))
                    i += 1
            self.objs = objs
        else:
            self.objs = []

    def env_replay(self, start_v:vertex, th_goal, obs_dict, steps):
        if not isinstance(th_goal, np.ndarray):
            th_goal = np.array(th_goal,dtype=np.float64)
        th = np.array(start_v.th,dtype=np.float64)
        w = start_v.w
        w = w.astype(np.float64)
        t_start = start_v.t
        t_start = int(t_start)
        # th, w, score, t, flag = env_replay(th,w,t_start, th_goal, obs_dict, steps)
        # self.th = th
        # self.w = w
        # self.jnt_err = calc_jnt_err(th, self.goal)
        # self.dedt = -1*w
        return env_replay(th,w,t_start, th_goal, obs_dict, steps,S,a,l)

    def reward(self, eef_vel, prox):
        return -1
    
    def step(self, action, use_PID=False, eval=False):  
        objs_arr = np.zeros((3,len(self.objs)), dtype=float)
        nxt_objs_arr = np.zeros_like(objs_arr, dtype=float)
        for i in range(len(self.objs)):
            o = self.objs[i]
            objs_arr[:,i] = o.path(self.t_step)
            nxt_objs_arr = o.path(self.t_step+1)
        
        if use_PID:
            err = self.jnt_err
            dedt = self.dedt
            action = PDControl(err, dedt)
            self.info['action'] = action
            package = nxt_state(objs_arr, self.th, self.w, action, a, l, S)
        else:
            if not isinstance(action,np.ndarray):
                action = action.detach().cpu().numpy()
                action = action.astype(np.float64)
                action = action.reshape(5)
            package = nxt_state(objs_arr, self.th, self.w, action, a, l, S)
        
        nxt_th = package[0:self.th.shape[0],0]
        nxt_w = package[0:self.w.shape[0],1]
        prox = package[-1,0]

        # need to add a eef vel function
        reward = self.reward(np.zeros(3), prox)
        
        self.t_step += 1
        err = calc_jnt_err(nxt_th, self.goal)
        if self.t_step*dt >= t_limit:
            done = True
        elif np.all(abs(err) < thres) and np.all(abs(nxt_w) < vel_thres):
            done = True
            # print('reached goal')
            reward += 10
        else:
            done = False

        self.th = nxt_th
        self.w = nxt_w
        self.jnt_err = calc_jnt_err(self.th, self.goal)
        self.dedt = -1*self.w
        coords,feats = [],[]
        if not eval:
            rob_coords, rob_feats = get_coords(nxt_th, self.t_step)
            for o in self.objs:
                c,f = o.get_coords(self.t_step)
                coords.append(c)
                feats.append(f)
            coords.append(rob_coords)
            feats.append(rob_feats)
            coords = np.vstack(coords)
            feats = np.vstack(feats)
        
        state = (coords, feats, self.jnt_err, self.dedt)
        return state, reward, done, self.info
    
    def get_state(self):
        coords,feats=[],[]
        rob_coords,rob_feats = get_coords(self.th, self.t_step)
        for o in self.objs:
            c,f = o.get_coords(self.t_step)
            coords.append(c)
            feats.append(f)
        coords.append(rob_coords)
        feats.append(rob_feats)
        coords = np.vstack(coords)
        feats = np.vstack(feats)

        state = (coords, feats, self.jnt_err, self.dedt)
        return state 

    def reset(self):
        self.th = self.start
        self.w = np.zeros_like(self.th, dtype=float)
        self.jnt_err = calc_jnt_err(self.th, self.goal)
        self.t_step = 0
        self.dedt = -1*self.w
        self.memory.clear()

    def store_transition(self,state, action, reward, new_state,done,t_step):
        self.memory.store_transition(state, action, reward,new_state,done,t_step)

    def sample_memory(self):
        return self.memory.sample_buffer(self.batch_size)

