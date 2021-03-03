from typing import Any, Callable, NamedTuple
import functools
import matplotlib.pyplot as plt
import jax
import jax.numpy as jnp
from jax.experimental import ode
from . import plot


class State(NamedTuple):
    v: jnp.ndarray
    w: jnp.ndarray
    u: jnp.ndarray


def forward(
    state,
    checkpoints,
    cell_parameters,
    diffusivity,
    stimuli,
    dt,
    dx,
    integrator="euler",
    plot_while=False,
):
    if plot_while:
        fig, ax = plot.plot_stimuli(stimuli)
        fig.suptitle("Stimuli")
        fig, ax = plot.plot_diffusivity(diffusivity)
        fig.suptitle("Diffusivity")
        fig, ax = plot.plot_state(state)
        fig.suptitle("Initial state")
        plt.show()

    integrator = integrator.lower()
    f = _forward_euler if integrator == "euler" else _forward_rk

    states = []
    for i in range(len(checkpoints) - 1):
        print(
            "Solving at: %dms/%dms\t\t with %d passages"
            % (
                checkpoints[i + 1] * dt,
                checkpoints[-1] * dt,
                checkpoints[i + 1] - checkpoints[i],
            ),
            end="\r",
        )
        state = f(
            state,
            float(checkpoints[i]),
            float(checkpoints[i + 1]),
            cell_parameters,
            diffusivity,
            stimuli,
            dt,
            dx,
        )
        if plot_while:
            plot.plot_state(state)
            plt.show()
        states.append(state)
    return states


@functools.partial(jax.jit, static_argnums=0)
def init(shape):
    v = jnp.ones(shape)
    w = jnp.ones(shape)
    u = jnp.zeros(shape)
    return State(v, w, u)


@jax.jit
def step(state, t, params, diffusivity, stimuli, dx):
    # neumann boundary conditions
    v = jnp.pad(state.v, 1, mode="edge")
    w = jnp.pad(state.w, 1, mode="edge")
    u = jnp.pad(state.u, 1, mode="edge")
    diffusivity = jnp.pad(diffusivity, 1, mode="edge")

    # reaction term
    p = jnp.greater_equal(u, params.V_c)
    q = jnp.greater_equal(u, params.V_v)
    tau_v_minus = (1 - q) * params.tau_v1_minus + q * params.tau_v2_minus

    j_fi = -v * p * (u - params.V_c) * (1 - u) / params.tau_d
    j_so = (u * (1 - p) / params.tau_0) + (p / params.tau_r)
    j_si = -(w * (1 + jnp.tanh(params.k * (u - params.V_csi)))) / (2 * params.tau_si)
    j_ion = -(j_fi + j_so + j_si) / params.Cm

    # apply stimulus by introducing fictitious current
    stimuli = [s._replace(field=jnp.pad(s.field, 1, mode="edge")) for s in stimuli]
    j_ion = stimulate(t, j_ion, stimuli)

    # diffusion term
    u_x = gradient(u, 0) / dx
    u_y = gradient(u, 1) / dx
    u_xx = gradient(u_x, 0) / dx
    u_yy = gradient(u_y, 1) / dx
    D_x = gradient(diffusivity, 0) / dx
    D_y = gradient(diffusivity, 1) / dx
    del_u = diffusivity * (u_xx + u_yy) + (D_x * u_x) + (D_y * u_y)

    d_v = ((1 - p) * (1 - v) / tau_v_minus) - ((p * v) / params.tau_v_plus)
    d_w = ((1 - p) * (1 - w) / params.tau_w_minus) - ((p * w) / params.tau_w_plus)
    d_u = del_u + j_ion

    return State(
        d_v[1:-1, 1:-1],
        d_w[1:-1, 1:-1],
        d_u[1:-1, 1:-1],
    )


def step_euler(state, t, params, diffusivity, stimuli, dt, dx):
    grads = step(state, t, params, diffusivity, stimuli, dx)
    return jax.tree_multimap(lambda v, dv: jnp.add(v, dv * dt), state, grads)


def step_rk(state, t, params, diffusivity, stimuli, dt, dx):
    return ode.odeint(step, state, t, params, diffusivity, stimuli, dx)


@functools.partial(jax.jit, static_argnums=1)
def gradient(a, axis):
    sliced = functools.partial(jax.lax.slice_in_dim, a, axis=axis)
    a_grad = jnp.concatenate(
        (
            # 3th order edge
            (
                (-11 / 6) * sliced(0, 2)
                + 3 * sliced(1, 3)
                - (3 / 2) * sliced(2, 4)
                + (1 / 3) * sliced(3, 5)
            ),
            # 4th order inner
            (
                (1 / 12) * sliced(None, -4)
                - (2 / 3) * sliced(1, -3)
                + (2 / 3) * sliced(3, -1)
                - (1 / 12) * sliced(4, None)
            ),
            # 3th order edge
            (
                (-1 / 3) * sliced(-5, -3)
                + (3 / 2) * sliced(-4, -2)
                - 3 * sliced(-3, -1)
                + (11 / 6) * sliced(-2, None)
            ),
        ),
        axis,
    )
    return a_grad


@jax.jit
def neumann(X):
    X = jnp.pad(X, 1, mode="edge")
    return X


@jax.jit
def stimulate(t, X, stimuli):
    stimulated = jnp.zeros_like(X)
    for stimulus in stimuli:
        # check if stimulus is in the past
        active = jnp.greater_equal(t, stimulus.protocol.start)
        # check if stimulus is active at the current time
        active &= (
            jnp.mod(stimulus.protocol.start - t + 1, stimulus.protocol.period)
            < stimulus.protocol.duration
        )
        # build the stimulus field
        stimulated = jnp.where(stimulus.field * (active), stimulus.field, stimulated)
    # set the field to the stimulus
    return jnp.where(stimulated != 0, stimulated, X)


@jax.jit
def _forward_euler(state, t, t_end, params, diffusivity, stimuli, dt, dx):
    # iterate
    state = jax.lax.fori_loop(
        t,
        t_end,
        lambda i, state: step_euler(state, i, params, diffusivity, stimuli, dt, dx),
        init_val=state,
    )
    return state


@functools.partial(jax.jit, static_argnums=(1, 2))
def _forward_rk(state, t, t_end, params, diffusivity, stimuli, dt, dx):
    return ode.odeint(
        step,
        state,
        jnp.array((t, t_end), dtype=float),
        params,
        diffusivity,
        stimuli,
        dx,
    )


def _forward_dormandprince(y0, ts, rtol=1.4e-8, atol=1.4e-8, mxstep=jnp.inf, *args):
    func_ = lambda y, t: step(y, t, *args)

    def scan_fun(carry, target_t):
        def cond_fun(state):
            i, _, _, t, dt, _, _ = state
            return (t < target_t) & (i < mxstep) & (dt > 0)

        def body_fun(state):
            i, y, f, t, dt, last_t, interp_coeff = state
            next_y, next_f, next_y_error, k = ode.runge_kutta_step(func_, y, f, t, dt)
            next_t = t + dt
            error_ratios = ode.error_ratio(next_y_error, rtol, atol, y, next_y)
            new_interp_coeff = ode.interp_fit_dopri(y, next_y, k, dt)
            dt = ode.optimal_step_size(dt, error_ratios)

            new = [i + 1, next_y, next_f, next_t, dt, t, new_interp_coeff]
            old = [i + 1, y, f, t, dt, last_t, interp_coeff]
            return map(
                functools.partial(jnp.where, jnp.all(error_ratios <= 1.0)), new, old
            )

        _, *carry = jax.lax.while_loop(cond_fun, body_fun, [0] + carry)
        _, _, t, _, last_t, interp_coeff = carry
        relative_output_time = (target_t - last_t) / (t - last_t)
        y_target = jnp.polyval(interp_coeff, relative_output_time)
        return carry, y_target

    f0 = func_(y0, ts[0])
    dt = ode.initial_step_size(func_, ts[0], y0, 4, rtol, atol, f0)
    interp_coeff = jnp.array([y0] * 5)
    init_carry = [y0, f0, ts[0], dt, ts[0], interp_coeff]
    _, ys = jax.lax.scan(scan_fun, init_carry, ts[1:])
    return jnp.concatenate((y0[None], ys))
