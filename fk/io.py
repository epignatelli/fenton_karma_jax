import h5py
import os


def init(path, shape, n_iter, n_stimuli):
    hdf5 = h5py.File(path, "w")
    if "states" not in hdf5:
        # shape is (t, 3, w, h), where 3 is the tree fk variable
        dset_states = hdf5.create_dataset("states", shape=(n_iter, 3, *shape), dtype="float32")
    if "stimuli" not in hdf5:
        dset_stim = hdf5.create_dataset("stimuli", shape=(n_stimuli, *shape), dtype="float32")
    return hdf5


def add_params(hdf5, params, diffusivity, dt, dx):
    hdf5.create_dataset("params/D", data=diffusivity)
    hdf5.create_dataset("params/dt", data=dt)
    hdf5.create_dataset("params/dx", data=dx)
    for key in params:
        hdf5.create_dataset("params/" + key, data=params[key])
    return True


def add_stimuli(hdf5, stimuli):
    hdf5.create_dataset("field", data=[stimuli[i]["field"] for i in range(len(stimuli))])
    hdf5.create_dataset("start", data=[stimuli[i]["start"] for i in range(len(stimuli))])
    hdf5.create_dataset("duration", data=[stimuli[i]["duration"] for i in range(len(stimuli))])
    hdf5.create_dataset("period", data=[stimuli[i]["period"] for i in range(len(stimuli))])
    return True
        
        
def append_states(hdf5, states, start, end):
    # shape is (t, 3, w, h), where 3 is the tree fk variable
    hdf5["states"][start:end] = states
    return True
        
    
def load(path, dataset, start, end, step=None):
    with h5py.File(path, "r") as file:
        return [file[dset][start:end:step] for dset in file]
    
    
def load_slice(dset, start, end, step):
    return [file[dset][start:end:step] for dset in file]
    