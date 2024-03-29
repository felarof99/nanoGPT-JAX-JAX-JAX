from typing import Tuple

import chex
from chex._src import fake
import jax
import jax.numpy as jnp
import optax
from flax.training import train_state
from dataclasses import dataclass
import importlib

import dataset
import model

class TrainState(train_state.TrainState):
    key: jax.random.KeyArray

@dataclass
class Config:
    BATCH_SIZE: int = 512
    BLOCK_SIZE: int = 64
    T: int = 64
    n_embed: int = 256
    num_heads: int = 8
    num_layers: int = 6

config = Config()

random_key = jax.random.PRNGKey(99)

# Initialize model
lm_model = model.LanguageModel(vocab_size=65, 
                      n_embed=config.n_embed, 
                      T=config.BLOCK_SIZE,
                      num_heads=config.num_heads,
                      num_layers=config.num_layers)
sample_block_of_tokens = jnp.ones(shape=(config.T,), dtype=jnp.int32)
output, params = lm_model.init_with_output(jax.random.PRNGKey(99), sample_block_of_tokens, training=False)
params = params["params"]

def model_apply(params, inputs, training, dropout_key):
    return lm_model.apply({"params": params}, inputs, training, rngs={'dropout': dropout_key})

# Vectorize model apply function
model_apply_batch = jax.vmap(model_apply, in_axes=(None, 0, None, None), out_axes=(0))

PER_HOST_BATCH_SIZE = config.BATCH_SIZE // jax.device_count()

# Define forward pass
def forward_pass(params, state, batch, dropout_key):
    inputs, targets = batch
    logits = state.apply_fn(params, inputs, True, dropout_key)

    chex.assert_shape(inputs, (PER_HOST_BATCH_SIZE, config.BLOCK_SIZE))
    chex.assert_shape(targets, (PER_HOST_BATCH_SIZE, config.BLOCK_SIZE))

    loss = optax.softmax_cross_entropy_with_integer_labels(logits, targets)
    loss = loss.mean()
    return loss

# Define training step
def train_step(state, inputs, targets, dropout_key):
    dropout_key = jax.random.fold_in(key=dropout_key, data=state.step)

    batch = inputs, targets

    grad_fn = jax.value_and_grad(forward_pass, argnums=(0))
    loss, grads = grad_fn(state.params, state, batch, dropout_key)

    print("ntn99 dropout key", dropout_key)
    print("ntn99 loss", loss)

    loss = jax.lax.pmean(loss, axis_name="devices")
    grads = jax.lax.pmean(grads, axis_name="devices")

    state = state.apply_gradients(grads=grads)
    return state, loss

# Initialize optimizer and training state
opt = optax.adam(learning_rate=0.0001)
state = TrainState.create(apply_fn=model_apply_batch, params=params, tx=opt, key=random_key)
data = dataset.Dataset(batch_size=config.BATCH_SIZE, block_size=config.BLOCK_SIZE)

# pmap the train_step.
train_step_pmap = jax.jit(jax.pmap(train_step, in_axes=(0, 0, 0, None), out_axes=(0), axis_name="devices"))
states = jax.device_put_replicated(state, jax.local_devices())

# Function to run a training step
# This is an **IMPURE function** for convenience. Don't JIT it.
def run_train_step(fake_jit = False, fake_pmap = False, reload_libs = False):
  if reload_libs:
    importlib.reload(dataset)
    importlib.reload(model)

  global state, states, random_key

  fake_pmap = chex.fake_pmap_and_jit(enable_jit_patching=fake_jit, enable_pmap_patching=fake_pmap)
  fake_pmap.start()

  num_epochs = 5
  steps_per_epoch = len(data.train_data) // config.BATCH_SIZE 
  for epoch in range(num_epochs):
    print("epoch: ", epoch)
    data.create_train_dataset()

    for step in range(steps_per_epoch):
      random_key, random_subkey = jax.random.split(random_key)

      inputs, targets = data.get_batch()

      # create device dimension for minibatch
      inputs = inputs.reshape((jax.device_count(), -1, inputs.shape[-1]))
      targets = targets.reshape((jax.device_count(), -1, targets.shape[-1]))

      states, loss = train_step_pmap(states, inputs, targets, random_subkey)
      print("loss", loss[0], "epoch", epoch) if epoch % 1000 == 0 else None

  fake_pmap.stop()

        
# if __name__ == "__main__":
#     run_train_step()