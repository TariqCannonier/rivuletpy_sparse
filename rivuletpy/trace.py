import os
import numpy as np
from scipy import ndimage 
from .utils.backtrack import *
from .utils.preprocessing import distgradient
import progressbar
from scipy.interpolate import RegularGridInterpolator 
from random import random
from skimage.morphology import skeletonize_3d
import skfmm

def makespeed(dt, threshold=0):
    F = dt ** 4
    F[F<=threshold] = 1e-10
    return F

def iterative_backtrack(t, bimg, somapt, somaradius, render=False, silence=False, eraseratio=1.2):
    '''Trace the 3d tif with a single neuron using Rivulet algorithm'''
    config = {'length':6, 'coverage':0.98, 'gap':15}

    # Get the gradient of the Time-crossing map
    dx, dy, dz = distgradient(t.astype('float64'))
    standard_grid = (np.arange(t.shape[0]), np.arange(t.shape[1]), np.arange(t.shape[2]))
    ginterp = (RegularGridInterpolator(standard_grid, dx),
               RegularGridInterpolator(standard_grid, dy),
               RegularGridInterpolator(standard_grid, dz))

    bounds = t.shape
    tt = t.copy()
    tt[bimg <= 0] = -2
    bb = np.zeros(shape=tt.shape) # For making a large tube to contain the last traced branch

    if render:
        from .utils.rendering3 import Viewer3, Line3, Ball3
        viewer = Viewer3(800, 800, 800)
        viewer.set_bounds(0, bounds[0], 0, bounds[1], 0, bounds[2])

    # Start tracing loop
    nforeground = bimg.sum()
    converage = 0.0
    iteridx = 0
    swc = None
    if not silence: bar = progressbar.ProgressBar(max_value=1.)
    velocity = None

    while converage < config['coverage']:
        iteridx += 1
        converage = np.logical_and(tt==-1, bimg > 0).sum() / nforeground

        # Find the geodesic furthest point on foreground time-crossing-map
        endpt = srcpt = np.asarray(np.unravel_index(tt.argmax(), tt.shape)).astype('float64')
        if not silence: bar.update(converage)

        # Trace it back to maxd 
        path = [srcpt,]
        reached = False
        touched = False
        gapctr = 0 # Count continous steps on background
        fgctr = 0 # Count how many steps are made on foreground in this branch
        steps_after_reach = 0
        outofbound = reachedsoma = False

        # For online confidence comupting
        online_voxsum = 0.
        low_online_conf = False

        line_color = [random(), random(), random()]

        while True: # Start 1 Back-tracking iteration
            try:
                endpt = rk4(srcpt, ginterp, t, 1)
                endptint = [math.floor(p) for p in endpt]
                velocity = endpt - srcpt

                # See if it travels too far on the background
                endpt_b = bimg[endptint[0], endptint[1], endptint[2]]
                gapctr = 0 if endpt_b else gapctr + 1
                fgctr += endpt_b

                # Compute the online confidence
                online_voxsum += endpt_b
                online_confidence = online_voxsum / (len(path) + 1)

                # if gapctr > config['gap']: break  # Stop tracing due to the gap threshold

                if np.linalg.norm(somapt - endpt) < 1.5 * somaradius: # Stop due to reaching soma point
                    reachedsoma = True
                    break

                # Render the line segment
                if render:
                    l = Line3(srcpt, endpt)
                    l.set_color(*line_color)
                    viewer.add_geom(l)
                    viewer.render(return_rgb_array=False)

                if not inbound(endpt, tt.shape): 
                    outofbound = True
                    break;
                if tt[endptint[0], endptint[1], endptint[2]] == -1:
                    reached = True

                if reached: # If the endpoint reached previously traced area check for node to connect for at each step
                    if swc is None: break;

                    steps_after_reach += 1
                    endradius = getradius(bimg, endpt[0], endpt[1], endpt[2])
                    touched, touchidx = match(swc, endpt, endradius)
                    closestnode = swc[touchidx, :]
                    if touched and render:
                        ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                        if len(path) < config['length']:
                            ball.set_color(1, 1, 1)
                        else:
                            ball.set_color(0, 0, 1)
                        viewer.add_geom(ball)
                    if touched or steps_after_reach >= 20: break

                if len(path) > 15 and np.linalg.norm(path[-15] - endpt) < 1.:
                    break;

                # if len(path) > config['length'] and online_confidence < 0.15:
                if online_confidence < 0.25:
                    low_online_conf = True
                    break 

            except ValueError:
                if velocity is not None:
                    endpt = srcpt + velocity
                break

            path.append(endpt)
            srcpt = endpt

        # Check forward confidence 
        cf = conf_forward(path, bimg)

        ## Erase it from the timemap
        rlist = []
        for node in path:
            n = [math.floor(n) for n in node]
            r = getradius(bimg, n[0], n[1], n[2])
            r = 1 if r < 1 else r
            rlist.append(r)
            
            # To make sure all the foreground voxels are included in bb
            r *= eraseratio
            r = math.ceil(r)
            X, Y, Z = np.meshgrid(constrain_range(n[0]-r, n[0]+r+1, 0, tt.shape[0]),
                                  constrain_range(n[1]-r, n[1]+r+1, 0, tt.shape[1]),
                                  constrain_range(n[2]-r, n[2]+r+1, 0, tt.shape[2]))
            bb[X, Y, Z] = 1

        startidx = [math.floor(p) for p in path[0]]
        endidx = [math.floor(p) for p in path[-1]]

        if len(path) > config['length'] and tt[endidx[0], endidx[1], endidx[2]] < tt[startidx[0], startidx[1], startidx[2]]:
            erase_region = np.logical_and(tt[endidx[0], endidx[1], endidx[2]] <= tt, tt <= tt[startidx[0], startidx[1], startidx[2]])
            erase_region = np.logical_and(bb, erase_region)
        else:
            erase_region = bb.astype('bool')

        if np.count_nonzero(erase_region) > 0:
            tt[erase_region] = -1
        bb.fill(0)
            
        # if len(path) > config['length']: 
        if touched:
            connectid = swc[touchidx, 0]
        elif reachedsoma:
            connectid = 1 
        else:
            connectid = None

        if cf[-1] < 0.5 or low_online_conf: # Check the confidence of this branch
            continue 

        swc = add2swc(swc, path, rlist, connectid)

    # Check all unconnected nodes
    for nodeidx in range(swc.shape[0]):
        if swc[nodeidx, -1]  == -2:
            # Find the closest node in swc, excluding the nodes traced earlier than this node in match
            swc2consider = swc[swc[:, 0] > swc[nodeidx, 0], :]
            connect, minidx = match(swc2consider, 
                                                     swc[nodeidx, 2:5], 3)
            if connect:
                swc[nodeidx, -1] = swc2consider[minidx, 0]
            else:
                swc[nodeidx, 1] = 200 

    # Prune short leaves 
    swc = prune_leaves(swc, bimg, config['length'], 0.5)

    # Add soma node to the result swc
    somanode = np.asarray([0, 1, somapt[0], somapt[1], somapt[2], somaradius, -1])
    swc = np.vstack((somanode, swc))

    return swc


