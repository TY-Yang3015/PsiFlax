from flax.training.train_state import TrainState
import jax.numpy as jnp
from clu import metric_writers
from jax import random, tree_map
from jax.lib import xla_bridge
from functools import partial
from omegaconf import DictConfig, OmegaConf
import jax
import logging
import optax
import hydra
import os
import orbax.checkpoint as ocp

import alderamin.folx as folx
from alderamin.shampoo.distributed_shampoo import distributed_shampoo as shampoo
from alderamin.backbone.models import PsiFormer
from alderamin.data import GlobalSystem
from alderamin.sampler import MetropolisHastingSampler
from alderamin.util import log_histograms


class PsiFormerTrainer:
    def __init__(self, config: DictConfig, system: GlobalSystem):
        self.config = config

        logger = logging.getLogger("main")
        logger.setLevel(logging.INFO)
        # log environment information
        logger.info(f"JAX backend: {xla_bridge.get_backend().platform}")

        logger.info(f"JAX process: {jax.process_index() + 1} / {jax.process_count()}")
        logger.info(f"JAX local devices: {jax.local_devices()}")

        # create sampler
        self.sampler = MetropolisHastingSampler(
            system=system,
            batch_size=self.config.hyperparam.batch_size,
            sampling_seed=self.config.sampler.sampling_seed,
            acceptance_range=self.config.sampler.acceptance_range,
            init_width=self.config.sampler.init_width,
            sample_width=self.config.sampler.sample_width,
            sample_width_adapt_freq=self.config.sampler.sample_width_adapt_freq,
            log_epsilon=self.config.hyperparam.log_epsilon,
            computation_dtype=self.config.sampler.computation_dtype,
            scale_input=self.config.hyperparam.scale_input,
        )

        # burn-in steps
        if self.config.sampler.burn_in_steps is not None:
            self.sampler.burn_in(self.config.sampler.burn_in_steps)

        # make some handy alias
        self.system = system
        self.num_of_electrons = self.system.total_electrons
        self.num_of_nucleus = self.system.total_nucleus
        self.nuc_charges = jnp.array([nuc.charge for nuc in self.system.nucleus_list])
        self.nuc_positions = jnp.array(
            [member.position for member in self.system.nucleus_list]
        )
        self.spins = jnp.array(
            [electron.spin for electron in self.system.electrons_list],
            dtype=jnp.float32,
        )

        # build neural network model
        self.psiformer = PsiFormer(
            num_of_determinants=self.config.psiformer.num_of_determinants,
            num_of_electrons=self.system.total_electrons,
            num_of_nucleus=self.system.total_nucleus,
            num_of_blocks=self.config.psiformer.num_of_blocks,
            num_heads=self.config.psiformer.num_heads,
            qkv_size=self.config.psiformer.qkv_size,
            use_memory_efficient_attention=self.config.psiformer.use_memory_efficient_attention,
            use_norm=self.config.psiformer.use_norm,
            group=self.config.psiformer.group,
            computation_dtype=self.config.psiformer.computation_dtype,
            param_dtype=self.config.psiformer.param_dtype,
            spins=self.spins,
            nuc_positions=self.nuc_positions,
            scale_input=self.config.hyperparam.scale_input,
        )

        # initialise optimiser
        def learning_rate_schedule(t_: jnp.ndarray) -> jnp.ndarray:
            return self.config.hyperparam.learning_rate * jnp.power(
                (1.0 / (1.0 + (t_ / self.config.hyperparam.delay))), self.config.hyperparam.decay)

        if self.config.hyperparam.optimiser.casefold() == 'adam':
            self.optimiser = optax.adam(learning_rate=learning_rate_schedule)
        elif self.config.hyperparam.optimiser.casefold() == 'shampoo':
            self.optimiser = shampoo(
                self.config.hyperparam.learning_rate,
                block_size=128,
                diagonal_epsilon=1e-12,
                matrix_epsilon=1e-12,
            )
        else:
            raise NotImplementedError(f"optimiser {self.config.hyperparam.optimiser} not available.")

        self.optimiser = optax.chain(
            optax.clip_by_global_norm(self.config.hyperparam.gradient_clipping),
            self.optimiser,
            #optax.scale_by_schedule(learning_rate_schedule),
            #optax.scale(-1.)
        )

    def _init_savedir(self) -> str:
        save_dir = hydra.core.hydra_config.HydraConfig.get().runtime.output_dir
        save_dir = str(os.path.join(save_dir, "results"))
        os.makedirs(save_dir)
        return save_dir

    @partial(jax.jit, static_argnums=0)
    def _train_step(self, batch, state):
        def get_electric_hamiltonian(coordinates: jnp.ndarray) -> jnp.ndarray:
            elec_elec_term = jnp.zeros((self.config.hyperparam.batch_size, 1))
            elec_nuc_term = jnp.zeros((self.config.hyperparam.batch_size, 1))
            nuc_nuc_term = jnp.zeros((self.config.hyperparam.batch_size, 1))

            for i in range(self.num_of_electrons):
                for j in range(i):
                    elec_elec_term = elec_elec_term.at[:, 0].add(
                        1.0
                        / (
                            jnp.linalg.norm(
                                coordinates[:, i, :] - coordinates[:, j, :], axis=-1
                            )
                        )
                    )

            for I in range(self.num_of_nucleus):
                for i in range(self.num_of_electrons):
                    elec_nuc_term = elec_nuc_term.at[:, 0].add(
                        (
                                self.nuc_charges[I]
                                / (
                                    jnp.linalg.norm(
                                        coordinates[:, i, :] - self.nuc_positions[I, :],
                                        axis=-1,
                                    )
                                )
                        )
                    )

            for I in range(self.num_of_nucleus):
                for J in range(I):
                    nuc_nuc_term = nuc_nuc_term.at[:, 0].add(
                        (self.nuc_charges[I] * self.nuc_charges[J])
                        / jnp.linalg.norm(
                            self.nuc_positions[I, :] - self.nuc_positions[J, :], axis=-1
                        )
                    )

            return elec_elec_term - elec_nuc_term + nuc_nuc_term

        def get_energy_and_grad(params):
            def get_wavefunction(raw_batch):
                wavefunction = state.apply_fn(
                    {"params": params},
                    raw_batch,
                )

                if wavefunction.shape == (1, 1):
                    wavefunction = wavefunction[0][0]

                return wavefunction

            electric_term = get_electric_hamiltonian(batch)

            laplacian_op = folx.forward_laplacian(get_wavefunction)
            result = jax.vmap(laplacian_op)(batch)
            laplacian, jacobian = result.laplacian, result.jacobian.dense_array
            kinetic_term = -(laplacian + jnp.square(jacobian).sum(-1)) / 2.

            #jacobian_op = jax.grad(get_wavefunction)
            #jacobian = jax.vmap(jacobian_op)(batch)
            #laplacian_op = jax.grad(lambda x: jacobian_op(x).sum())
            #laplacian = jax.vmap(laplacian_op)(batch)
            #kinetic_term = -(laplacian.sum(axis=(-1, -2))
            #                 ) / 2.

            kinetic_term = kinetic_term.reshape(-1, 1)

            energy_batch = kinetic_term + electric_term
            #energy_batch = jnp.clip(energy_batch, None, 0.)

            mean_absolute_deviation = jnp.mean(
                jnp.abs(energy_batch - jnp.median(energy_batch))
            )
            n = self.config.hyperparam.mad_clipping_factor
            energy_batch = jnp.clip(
                energy_batch,
                jnp.median(energy_batch) - (n * mean_absolute_deviation),
                jnp.median(energy_batch) + (n * mean_absolute_deviation),
            )

            def param_to_wavefunction(param_tree):
                wavefunction = state.apply_fn(
                    {"params": param_tree},
                    batch,
                )

                return wavefunction.squeeze(-1)

            def grad_func(x, i):
                return jax.grad(lambda f: param_to_wavefunction(f)[i])(x)

            # Use jax.vmap to vectorize over the index i
            vector_grad = jax.vmap(grad_func, in_axes=(None, 0))

            grad_log = vector_grad(params, jnp.arange(self.config.hyperparam.batch_size))
            #print(grad_log)

            mean_energy = energy_batch.mean()

            def one_grad(energy, one_tree):
                return tree_map(lambda g: g * (energy.squeeze(-1) - mean_energy), one_tree)

            batch_grad = jax.vmap(one_grad)(energy_batch, grad_log)

            grad_mean = tree_map(lambda g: 2. * jnp.mean(g, axis=0), batch_grad)

            return energy_batch.mean(), grad_mean

        energy, grad = get_energy_and_grad(state.params)

        state = state.apply_gradients(grads=grad)

        return state, energy, grad

    def train(self):

        logging.info("initializing model.")
        init_elec = jnp.ones(
            (
                self.config.hyperparam.batch_size,
                self.system.total_electrons,
                3,
            ),
            jnp.float32,
        )

        rngs = {"params": random.PRNGKey(self.config.hyperparam.training_seed)}
        params = jax.jit(self.psiformer.init)(rngs, init_elec)["params"]

        state = TrainState.create(
            apply_fn=self.psiformer.apply,
            params=params,
            tx=self.optimiser,
        )

        sharding = jax.sharding.NamedSharding(
            mesh=jax.sharding.Mesh(jax.devices(), axis_names="model"),
            spec=jax.sharding.PartitionSpec(),
        )

        create_sharded_array = lambda x: jax.device_put(x, sharding)
        state = jax.tree_util.tree_map(create_sharded_array, state)

        save_dir = self._init_savedir()

        if self.config.ckpt.save_ckpt:
            save_vae_path = ocp.test_utils.erase_and_create_empty(
                os.path.abspath(save_dir + "/ckpt")
            )

            save_options = ocp.CheckpointManagerOptions(
                max_to_keep=self.config.ckpt.save_num_ckpt,
                save_interval_steps=self.config.ckpt.ckpt_freq,
            )

            mngr = ocp.CheckpointManager(
                save_vae_path, options=save_options, item_names=("state", "config")
            )

        writer = metric_writers.SummaryWriter(logdir=save_dir)
        logger = logging.getLogger("loop")

        for step in range(self.config.hyperparam.step):
            batch = self.sampler.sample_psiformer(
                state, self.config.sampler.sample_steps
            )
            state, energy, grad = self._train_step(batch, state)

            if self.config.ckpt.save_ckpt:
                config_dict = OmegaConf.to_container(self.config, resolve=True)
                mngr.save(
                    step,
                    args=ocp.args.Composite(
                        state=ocp.args.StandardSave(state),
                        config=ocp.args.JsonSave(config_dict),
                    ),
                )

            log_histograms(writer, state.params, grad, step)
            writer.write_scalars(step, {"energy": energy})

            logger.info(f"step: {step} " f"energy: {energy}" f"")

        writer.flush()

        return state
