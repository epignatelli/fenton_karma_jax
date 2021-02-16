from functools import partial
from typing import Tuple

import jax
import jax.numpy as jnp
from cardiax import fk, ode
from cardiax.ode import stimulus

Shape = Tuple[int, ...]
Key = jnp.ndarray


def random_protocol(
    rng: Key,
    min_start: int = 0,
    max_start: int = 1000,
    min_period: int = 400,
    max_period: int = 1e9,
) -> stimulus.Protocol:
    rng_1, rng_2 = jax.random.split(rng)
    start = jax.random.randint(rng_1, (1,), min_start, max_start)
    duration = 2  # always instantaneous
    period = jax.random.randint(rng_2, (1,), min_period, max_period)
    return stimulus.Protocol(start, duration, period)


def random_rectangular_stimulus(
    rng: Key, shape: Shape, protocol: stimulus.Protocol, modulus: float = 0.6
) -> stimulus.Stimulus:
    rng_1, rng_2 = jax.random.split(rng)
    xs = jax.random.randint(rng_1, (2,), 0, shape[0] // 3)
    ys = jax.random.randint(rng_2, (2,), 0, shape[0] // 3)
    centre = (xs[0], ys[0])
    size = (xs[1], ys[1])
    return stimulus.rectangular(shape, centre, size, modulus, protocol)


def random_linear_stimulus(
    rng: Key, shape: Shape, protocol: stimulus.Protocol, modulus: float = 0.6
) -> stimulus.Stimulus:
    rng_1, rng_2 = jax.random.split(rng)
    direction = jax.random.randint(rng_1, (1,), 0, 3)
    coverage = jax.random.normal(rng_2, (1,))
    return stimulus.linear(shape, direction, coverage, modulus, protocol)


def random_triangular_stimulus(
    rng: Key, shape: Shape, protocol: stimulus.Protocol, modulus: float = 0.6
) -> stimulus.Stimulus:
    rng_1, rng_2 = jax.random.split(rng)
    angle, coverage = jax.random.normal(rng_1, (2,))
    direction = jax.random.randint(rng_1, (1,), 0, 3)
    return stimulus.triangular(shape, direction, angle, coverage, modulus, protocol)


def random_stimulus(rng: Key, shape: Shape) -> stimulus.Stimulus:
    stimuli_fn = (
        random_rectangular_stimulus,
        random_triangular_stimulus,
        random_linear_stimulus,
    )
    rng_1, rng_2, rng_3, rng_4 = jax.random.split(rng, 4)
    protocol = random_protocol(rng_1)
    modulus = jax.random.normal(rng_2, (1,))
    stimulus_fn = partial(
        jax.random.choice(rng_3, stimuli_fn),
        shape=shape,
        protocol=protocol,
        modulus=modulus,
    )
    return stimulus_fn(rng_4)


def random_gaussian_1d(rng: Key, length: int):
    rngs = jax.random.split(rng, 3)
    mu = jax.random.normal(rngs[0], (1,)) * 0.2
    sigma = jax.random.normal(rngs[1], (1,)) * 0.3
    x = jnp.linspace(-1, 1, length)
    return jnp.exp(-jnp.power(x - mu, 2.0) / (2 * jnp.power(sigma, 2.0)))


def random_gaussian_2d(rng: Key, shape: Shape):
    rngs = jax.random.split(rng, len(shape))
    x0 = random_gaussian_1d(rngs[0], shape[0])
    x1 = random_gaussian_1d(rngs[1], shape[1])
    return 1 - (jnp.ones(shape) * x0).T * x1


def random_gaussian_mixture(rng: Key, shape: Shape, n_gaussians: int) -> jnp.ndarray:
    rngs = jax.random.split(rng, n_gaussians)
    mixture = [random_gaussian_2d(rngs[i], shape) for i in range(n_gaussians)]
    return sum(mixture) / n_gaussians


def random_diffusivity(rng: Key, shape: Shape, n_gaussians: int = 3) -> jnp.ndarray:
    return random_gaussian_mixture(rng, shape, n_gaussians)


def random_sequence(
    rng,
    shape,
    n_stimuli,
    dt,
    dx,
    cell_parameters,
    reshape=None,
    save_interval_ms=1,
):
    """
    Generates a new spatio-temporal sequence of fenton-karma simulations.
    Args:
        start (float): start time of the simulation in millisecons
        stop (float): end time of the simulation in milliseconds
        dt (float): time infinitesimal for temporal gradients calculation (nondimensional)
        dx (float): space infinitesimal for spatial gradients calculation (nondimensional)
        cell_parameters (Dict[string, float]): set of electrophysiological parameters to use for the simulation
                                             Units are cm for space, ms for time, mV for potential difference, uF for capacitance
    """
    # generate random stimuli
    rngs = jax.random.split(rng, n_stimuli)
    stimuli = [random_stimulus(rngs[i], shape) for i in range(n_stimuli)]

    # generate diffusivity map
    rng = jax.random.split(rngs[-1], n_stimuli)
    diffusivity = random_diffusivity()


def sequence(
    start,
    stop,
    dt,
    dx,
    cell_parameters,
    diffusivity,
    stimuli,
    filename,
    reshape=None,
    save_interval_ms=1,
):
    """
    Generates a new spatio-temporal sequence of fenton-karma simulations.
    Args:
        start (float): start time of the simulation in millisecons
        stop (float): end time of the simulation in milliseconds
        dt (float): time infinitesimal for temporal gradients calculation (nondimensional)
        dx (float): space infinitesimal for spatial gradients calculation (nondimensional)
        cell_parameters (Dict[string, float]): set of electrophysiological parameters to use for the simulation
                                             Units are cm for space, ms for time, mV for potential difference, uF for capacitance
        diffusivity (np.ndarray): diffusivity map for tissue conduction velocity. Spatial units are in simulation units,
                                  and the value is the dimensional value, usually set to 0.001 cm^2/ms
        stimuli (List[Dict[string, object]]): The stimulus as a dictionary containing:
                                              "field": (np.ndarray) [mV/unitgrid],
                                              "start": (int) [ms],
                                              "duration": (int) [ms],
                                              "period": (int) [ms]
        filename (str): file path to save the simulation to. Simulation is saved with steps of 1ms
    """
    # check shapes
    for s in stimuli:
        assert (
            diffusivity.shape == s.field.shape
        ), "Inconsistend stimulus shapes {} and diffusivity {}".format(
            diffusivity.shape, s.field.shape
        )

    # checkpoints
    checkpoints = jnp.arange(
        int(start), int(stop), int(save_interval_ms / dt)
    )  # this guarantees a checkpoint every ms

    # shapes
    shape = diffusivity.shape
    tissue_size = fk.convert.shape_to_realsize(shape, dx)

    # show
    print("Tissue size", tissue_size, "Grid size", diffusivity.shape)
    print("Checkpointing at:", checkpoints)
    print("Cell parameters", cell_parameters)
    ode.plot.plot_stimuli(*stimuli)

    # init storage
    init_size = reshape or shape
    hdf5 = fk.io.init(
        filename, init_size, n_iter=len(checkpoints), n_stimuli=len(stimuli)
    )
    fk.io.add_params(hdf5, cell_parameters, diffusivity, dt, dx, shape=reshape)
    fk.io.add_stimuli(hdf5, stimuli, shape=reshape)

    states_dset = hdf5["states"]
    state = fk.solve.init(shape)
    for i in range(len(checkpoints) - 1):
        print(
            "Solving at: %dms/%dms\t\t"
            % (
                fk.convert.units_to_ms(checkpoints[i + 1], dt),
                fk.convert.units_to_ms(checkpoints[-1], dt),
            ),
            end="\r",
        )
        state = ode.forward._forward(
            state,
            checkpoints[i],
            checkpoints[i + 1],
            cell_parameters,
            diffusivity,
            stimuli,
            dt,
            dx,
        )
        fk.io.add_state(states_dset, state, i, shape=(len(state), *reshape))

    print()
    hdf5.close()
