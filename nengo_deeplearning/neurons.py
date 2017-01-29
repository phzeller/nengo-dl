from nengo.neurons import RectifiedLinear, Sigmoid, LIF, LIFRate
from nengo.builder.neurons import SimNeurons
import numpy as np
import tensorflow as tf

from nengo_deeplearning import utils, DEBUG
from nengo_deeplearning.builder import Builder, OpBuilder

# the neuron types for which we have a custom tensorflow implementation
TF_NEURON_IMPL = (RectifiedLinear, Sigmoid, LIF, LIFRate)


@Builder.register(SimNeurons)
class SimNeuronsBuilder(OpBuilder):
    def __init__(self, ops, signals):
        if DEBUG:
            print("sim_neurons")
            print([op for op in ops])
            print("J", [op.J for op in ops])

        neuron_type = type(ops[0].neurons)

        # if we have a custom tensorflow implementation for this neuron type,
        # then we build that. otherwise we'll just execute the neuron step
        # function externally (using `tf.py_func`), so we just need to set up
        # the inputs/outputs for that.
        if neuron_type in TF_NEURON_IMPL:
            # note: we do this two-step check (even though it's redundant) to
            # make sure that TF_NEURON_IMPL is kept up to date

            if neuron_type == RectifiedLinear:
                self.built_neurons = RectifiedLinearBuilder(ops, signals)
            if neuron_type == Sigmoid:
                self.built_neurons = SigmoidBuilder(ops, signals)
            elif neuron_type == LIFRate:
                self.built_neurons = LIFRateBuilder(ops, signals)
            elif neuron_type == LIF:
                self.built_neurons = LIFBuilder(ops, signals)
        else:
            self.built_neurons = GenericNeuronBuilder(ops, signals)

    def build_step(self, signals):
        self.built_neurons.build_step(signals)


class GenericNeuronBuilder(object):
    def __init__(self, ops, signals):
        self.J_data = signals.combine([op.J for op in ops])
        self.output_data = signals.combine([op.output for op in ops])

        # we combine all the state signals into a single tensor
        self.state_data = signals.combine([s for op in ops
                                           for s in op.states])

        def neuron_step_math(dt, J, states):
            output = None
            J_offset = 0
            state_offset = 0
            for i, op in enumerate(ops):
                # slice out the individual state vectors from the overall
                # array
                op_J = J[J_offset:J_offset + op.J.shape[0]]
                J_offset += op.J.shape[0]

                op_states = []
                for s in op.states:
                    op_states += [
                        states[state_offset:state_offset + s.shape[0]]]
                    state_offset += s.shape[0]

                # call step_math function
                # note: `op_states` are views into `states`, which will
                # be updated in-place
                mini_out = []
                for j in range(signals.minibatch_size):
                    # blank output variable
                    neuron_output = np.zeros(
                        op.output.shape, self.output_data.dtype)
                    op.neurons.step_math(dt, op_J[..., j], neuron_output,
                                         *[s[..., j] for s in op_states])
                    mini_out += [neuron_output]
                neuron_output = np.stack(mini_out, axis=-1)

                # concatenate outputs
                if output is None:
                    output = neuron_output
                else:
                    output = np.concatenate((output, neuron_output),
                                            axis=0)

            return output, states

        self.neuron_step_math = neuron_step_math
        self.neuron_step_math.__name__ = utils.sanitize_name(
            "_".join([repr(op.neurons) for op in ops]))

    def build_step(self, signals):
        J = signals.gather(self.J_data)
        states = ([] if self.state_data == [] else
                  [signals.gather(self.state_data)])
        states_dtype = ([] if self.state_data == [] else
                        [self.state_data.dtype])

        neuron_out, state_out = tf.py_func(
            self.neuron_step_math, [signals.dt, J] + states,
                                   [self.output_data.dtype] + states_dtype,
            name=self.neuron_step_math.__name__)
        neuron_out.set_shape(
            self.output_data.shape + (signals.minibatch_size,))
        state_out.set_shape(
            self.state_data.shape + (signals.minibatch_size,))

        signals.scatter(self.output_data, neuron_out)
        if self.state_data is not None:
            signals.scatter(self.state_data, state_out)


class RectifiedLinearBuilder(object):
    def __init__(self, ops, signals):
        self.J_data = signals.combine([op.J for op in ops])
        self.output_data = signals.combine([op.output for op in ops])

    def build_step(self, signals):
        J = signals.gather(self.J_data)
        signals.scatter(self.output_data, tf.nn.relu(J))


class SigmoidBuilder(object):
    def __init__(self, ops, signals):
        self.J_data = signals.combine([op.J for op in ops])
        self.output_data = signals.combine([op.output for op in ops])
        self.tau_ref = tf.constant(
            [[op.neurons.tau_ref] for op in ops
             for _ in range(op.J.shape[0])], dtype=signals.dtype)

    def build_step(self, signals):
        J = signals.gather(self.J_data)
        signals.scatter(self.output_data, tf.nn.sigmoid(J) / self.tau_ref)


class LIFRateBuilder(object):
    def __init__(self, ops, signals):
        self.tau_ref = tf.constant(
            [[op.neurons.tau_ref] for op in ops
             for _ in range(op.J.shape[0])], dtype=signals.dtype)
        self.tau_rc = tf.constant(
            [[op.neurons.tau_rc] for op in ops
             for _ in range(op.J.shape[0])], dtype=signals.dtype)

        self.J_data = signals.combine([op.J for op in ops])
        self.output_data = signals.combine([op.output for op in ops])
        self.zeros = tf.zeros(self.J_data.shape + (signals.minibatch_size,),
                              signals.dtype)

    def build_step(self, signals):
        J = signals.gather(self.J_data)
        j = J - 1

        # indices = tf.cast(tf.where(j > 0), tf.int32)
        # tau_ref = tf.gather_nd(self.tau_ref, tf.expand_dims(indices[:, 0], 1))
        # tau_rc = tf.gather_nd(self.tau_rc, tf.expand_dims(indices[:, 0], 1))
        # j = tf.gather_nd(j, indices)
        #
        # signals.scatter(
        #     self.output_data,
        #     tf.scatter_nd(indices, 1 / (tau_ref + tau_rc * tf.log1p(1 / j)),
        #                   tf.shape(J)))

        rates = 1 / (self.tau_ref + self.tau_rc * tf.log1p(1 / j))
        signals.scatter(self.output_data, tf.where(j > 0, rates, self.zeros))


class LIFBuilder(object):
    def __init__(self, ops, signals):
        self.tau_ref = tf.constant(
            [[op.neurons.tau_ref] for op in ops
             for _ in range(op.J.shape[0])], dtype=signals.dtype)
        self.tau_rc = tf.constant(
            [[op.neurons.tau_rc] for op in ops
             for _ in range(op.J.shape[0])], dtype=signals.dtype)
        self.min_voltage = tf.constant(
            [[op.neurons.min_voltage] for op in ops
             for _ in range(op.J.shape[0])], dtype=signals.dtype)

        self.J_data = signals.combine([op.J for op in ops])
        self.output_data = signals.combine([op.output for op in ops])
        self.voltage_data = signals.combine([op.states[0] for op in ops])
        self.refractory_data = signals.combine([op.states[1] for op in ops])
        self.zeros = tf.zeros(self.J_data.shape + (signals.minibatch_size,),
                              signals.dtype)

    def build_step(self, signals):
        # TODO: use sparse operators when dealing with spikes

        J = signals.gather(self.J_data)
        voltage = signals.gather(self.voltage_data)
        refractory = signals.gather(self.refractory_data)

        refractory -= signals.dt
        delta_t = tf.clip_by_value(signals.dt - refractory, 0, signals.dt)

        voltage -= (J - voltage) * (tf.exp(-delta_t / self.tau_rc) - 1)

        spiked = voltage > 1
        spikes = tf.cast(spiked, signals.dtype) / signals.dt
        signals.scatter(self.output_data, spikes)

        # note: this scatter/gather approach is slower than just doing the
        # computation on the whole array (even though we're not using the
        # result for any of the neurons that didn't spike). there may be some
        # cases where this is more efficient? (e.g. for large, sparse arrays)
        # indices = tf.cast(tf.where(spiked), tf.int32)
        # tau_rc = tf.gather_nd(self.tau_rc, tf.expand_dims(indices[:, 0], 1))
        # tau_ref = tf.gather_nd(
        #     self.tau_ref, tf.expand_dims(indices[:, 0], 1))
        # J = tf.gather_nd(J, indices)
        # t_spike = tau_ref + signals.dt + tau_rc * tf.log1p(
        #     -(tf.gather_nd(voltage, indices) - 1) / (J - 1))
        # refractory = tf.where(
        #     spiked, tf.scatter_nd(indices, t_spike, tf.shape(refractory)),
        #     refractory)

        t_spike = (self.tau_ref + signals.dt +
                   self.tau_rc * tf.log1p((1 - voltage) / (J - 1)))
        refractory = tf.where(spiked, t_spike, refractory)

        signals.scatter(self.refractory_data, refractory)

        voltage = tf.where(spiked, self.zeros,
                           tf.maximum(voltage, self.min_voltage))
        signals.scatter(self.voltage_data, voltage)
