import os
import argparse

import tensorflow as tf
from baselines.ppo2 import ppo2
from baselines.common.models import build_impala_cnn
from baselines.common.mpi_util import setup_mpi_gpus
from baselines.common.vec_env import VecExtractDictObs, VecMonitor, VecNormalize
from baselines import logger
from procgen import ProcgenEnv
from mpi4py import MPI

from .netrand_model import NetRandModel
from .netrand_runner import NetRandRunner
from .netrand_policy import build_policy

LOG_DIR = '~/procgen_exp/netrand'

def main():
    # Hyperparameters
    num_envs = 128
    learning_rate = 5e-4
    ent_coef = .01
    vf_coef = 0.5
    gamma = .999
    lam = .95
    nsteps = 256
    nminibatches = 8
    ppo_epochs = 3
    clip_range = .2
    max_grad_norm = 0.5
    timesteps_per_proc = 100_000_000
    use_vf_clipping = True

    # Parse arguments
    parser = argparse.ArgumentParser(description='Process procgen training arguments.')
    parser.add_argument('--env_name', type=str, default='coinrun')
    parser.add_argument('--distribution_mode', type=str, default='hard',
                        choices=["easy", "hard", "exploration", "memory", "extreme"])
    parser.add_argument('--num_levels', type=int, default=0)
    parser.add_argument('--start_level', type=int, default=0)
    parser.add_argument('--test_worker_interval', type=int, default=0)
    parser.add_argument('--run_id', type=int, default=1)
    parser.add_argument('--gpus_id', type=str, default='')
    args = parser.parse_args()

    # Setup test worker
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    test_worker_interval = args.test_worker_interval
    is_test_worker = False
    if test_worker_interval > 0:
        is_test_worker = comm.Get_rank() % test_worker_interval == (test_worker_interval - 1)
    mpi_rank_weight = 0 if is_test_worker else 1

    # Setup env specs
    env_name = args.env_name
    num_levels = 0 if is_test_worker else args.num_levels
    start_level = args.start_level

    # Setup logger
    log_comm = comm.Split(1 if is_test_worker else 0, 0)
    format_strs = ['csv', 'stdout'] if log_comm.Get_rank() == 0 else []
    logger.configure(dir=LOG_DIR + f'/{args.env_name}/run_{args.run_id}', format_strs=format_strs)

    # Create env
    logger.info("creating environment")
    venv = ProcgenEnv(num_envs=num_envs, env_name=env_name, num_levels=num_levels,
                      start_level=start_level, distribution_mode=args.distribution_mode)
    venv = VecExtractDictObs(venv, "rgb")
    venv = VecMonitor(venv=venv, filename=None, keep_buf=100)
    venv = VecNormalize(venv=venv, ob=False)

    # Setup Tensorflow
    logger.info("creating tf session")
    if args.gpus_id:
        gpus_id = [x.strip() for x in args.gpus_id.split(',')]
        os.environ["CUDA_VISIBLE_DEVICES"] = gpus_id[rank]
    setup_mpi_gpus()
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True #pylint: disable=E1101
    sess = tf.Session(config=config)
    sess.__enter__()

    # Setup model
    conv_fn = lambda x: build_impala_cnn(x, depths=[16,32,32])

    # Training
    logger.info("training")
    ppo2.Runner = NetRandRunner
    ppo2.build_policy = build_policy
    model = ppo2.learn(
        env=venv,
        network=conv_fn,
        total_timesteps=timesteps_per_proc,
        save_interval=0,
        nsteps=nsteps,
        nminibatches=nminibatches,
        lam=lam,
        gamma=gamma,
        noptepochs=ppo_epochs,
        log_interval=1,
        ent_coef=ent_coef,
        mpi_rank_weight=mpi_rank_weight,
        clip_vf=use_vf_clipping,
        comm=comm,
        lr=learning_rate,
        cliprange=clip_range,
        update_fn=None,
        init_fn=None,
        vf_coef=vf_coef,
        max_grad_norm=max_grad_norm,
        model_fn=NetRandModel,
    )

    # Saving
    logger.info("saving final model")
    if rank == 0:
        checkdir = os.path.join(logger.get_dir(), 'checkpoints')
        os.makedirs(checkdir, exist_ok=True)
        model.save(os.path.join(checkdir, 'final_model.ckpt'))

if __name__ == '__main__':
    main()
