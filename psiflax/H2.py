import hydra

from psiflax.data import GlobalSystem, AtomicNucleus, ElectronNucleusSystem
from psiflax.trainer import PsiFormerTrainer

from omegaconf import DictConfig

a = AtomicNucleus("H", (0, 0, 0))
b = ElectronNucleusSystem(system_nucleus=a, num_electrons=1).initialize_system()
c = AtomicNucleus("H", (1.4011, 0, 0))
d = ElectronNucleusSystem(system_nucleus=c, num_electrons=1).initialize_system()
e = GlobalSystem(system_member=[b, d]).initialize_system()


@hydra.main(version_base=None, config_path="./config", config_name="base_config")
def execute(config: DictConfig) -> None:

    trainer = PsiFormerTrainer(config, e)

    pos = trainer.sampler.walker_state.positions
    print(pos.shape)
    import matplotlib.pyplot as plt
    from einops import rearrange

    pos = rearrange(pos, "b i j -> (b i) j")
    fig, ax = plt.subplots(figsize=(12, 12))
    ax.set_xlim(-1, 3)
    ax.set_ylim(-2, 2)
    ax.plot(pos[:, 0], pos[:, 1], ".", ms=1)
    plt.show()

    state = trainer.train()

    pos = trainer.sampler.walker_state.positions
    print(pos.shape)
    import matplotlib.pyplot as plt
    from einops import rearrange
    import numpy as np

    # xy_pos = rearrange(pos, 'b n i -> (b n) i')
    xy_pos = pos[:, 0, :]
    x = xy_pos[:, 0]
    y = xy_pos[:, 1]

    z = np.square(np.exp(state.apply_fn({"params": state.params}, pos)))[0]
    # z = np.clip(z, 0, 10)
    plt.figure(figsize=(12, 12))

    ax = plt.gca()
    ax.scatter(pos[:, 1, 0], pos[:, 1, 1], c=z, cmap="viridis", s=5)
    ax.scatter(pos[:, 0, 0], pos[:, 0, 1], c=z, cmap="viridis", s=5)
    ax.set_xlim(-1, 3)
    ax.set_ylim(-2, 2)

    plt.show()


if __name__ == "__main__":
    execute()
