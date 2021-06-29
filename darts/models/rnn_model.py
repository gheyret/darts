"""
Recurrent Neural Networks
-------------------------
"""

import torch.nn as nn
import torch
from torch.utils.data import DataLoader
from numpy.random import RandomState
from joblib import Parallel, delayed
from typing import Sequence, Optional, Union
from ..timeseries import TimeSeries

from ..logging import raise_if_not, get_logger
from .torch_forecasting_model import TorchForecastingModel, TimeSeriesTorchDataset
from ..utils.torch import random_method
from ..utils.data.timeseries_dataset import TimeSeriesInferenceDataset
from ..utils.data import ShiftedDataset
from ..utils import _build_tqdm_iterator

logger = get_logger(__name__)


# TODO add batch norm
class _RNNModule(nn.Module):
    def __init__(self,
                 name: str,
                 input_size: int,
                 hidden_dim: int,
                 target_size: int = 1,
                 dropout: float = 0.,
                 probabilistic: bool = False):

        """ PyTorch module implementing an RNN to be used in `RNNModel`.

        PyTorch module implementing a simple RNN with the specified `name` type.
        This module combines a PyTorch RNN module, together with one fully connected layer which
        maps the hidden state of the RNN at each step to the output value of the model at that
        time step.

        Parameters
        ----------
        name
            The name of the specific PyTorch RNN module ("RNN", "GRU" or "LSTM").
        input_size
            The dimensionality of the input time series.
        hidden_dim
            The number of features in the hidden state `h` of the RNN module.
        target_size
            The dimensionality of the output time series.
        dropout
            The fraction of neurons that are dropped in all-but-last RNN layers.
        probabilistic
            Boolean value indicating whether forecasts should be probability distributions
            instead of point predictions. For now, only the Gaussian distribution is supported.

        Inputs
        ------
        x of shape `(batch_size, input_length, input_size)`
            Tensor containing the features of the input sequence. The `input_length` is not fixed.

        Outputs
        -------
        y of shape `(batch_size, input_length, output_size)`
            Tensor containing the outputs of the RNN at every time step of the input sequence.
            During training the whole tensor is used as output, whereas during prediction we only use y[:, -1, :].
            However, this module always returns the whole Tensor.
        """

        super(_RNNModule, self).__init__()

        # Defining parameters
        self.target_size = target_size
        self.name = name
        self.is_probabilistic = probabilistic

        # Defining the RNN module
        self.rnn = getattr(nn, name)(input_size, hidden_dim, 1, batch_first=True, dropout=dropout)
        # TODO: dropout is not used currently since the pytorch module doesn't adds it to the last layer and we have
        # just 1 layer so no dropout is added

        # The RNN module needs a linear layer V that transforms hidden states into outputs, individually
        self.V = nn.Linear(hidden_dim, target_size + int(probabilistic) * target_size)

    def forward(self, x, h=None):
        # data is of size (batch_size, input_length, input_size)
        batch_size = x.size(0)

        # out is of size (batch_size, input_length, hidden_dim)
        out, last_hidden_state = self.rnn(x) if h is None else self.rnn(x, h)

        # Here, we apply the V matrix to every hidden state to produce the outputs
        predictions = self.V(out)

        # predictions is of size (batch_size, input_length, target_size)
        predictions = predictions.view(batch_size, -1, self.target_size + int(self.is_probabilistic) * self.target_size)

        # returns outputs for all inputs, only the last one is needed for prediction time
        return predictions, last_hidden_state


class RNNModel(TorchForecastingModel):
    @random_method
    def __init__(self,
                 model: Union[str, nn.Module] = 'RNN',
                 input_chunk_length: int = 12,
                 hidden_dim: int = 25,
                 dropout: float = 0.,
                 training_length: int = 24,
                 probabilistic: bool = False,
                 random_state: Optional[Union[int, RandomState]] = None,
                 **kwargs):

        """ Recurrent Neural Network Model (RNNs).

        This class provides three variants of RNNs:

        * Vanilla RNN

        * LSTM

        * GRU

        RNNModel is fully recurrent in the sense that, at prediction time, an output is computed using these inputs:
        - previous target value, which will be set to the last known target value for the first prediction,
          and for all other predictions it will be set to the previous prediction
        - the previous hidden state
        - the current covariates (if the model was trained with covariates)
        
        For a block version using an RNN model as an encoder only, checkout `BlockRNNModel`.

        Parameters
        ----------
        model
            Either a string specifying the RNN module type ("RNN", "LSTM" or "GRU"),
            or a PyTorch module with the same specifications as
            `darts.models.rnn_model.RNNModule`.
        input_chunk_length
            Number of past time steps that are fed to the forecasting module at prediction time.
        hidden_dim
            Size for feature maps for each hidden RNN layer (:math:`h_n`).
        dropout
            Fraction of neurons afected by Dropout.
        training_length
            The length of both input (target and covariates) and output (target) time series used during
            training. Generally speaking, `training_length` should have a higher value than `input_chunk_length`
            because otherwise during training the RNN is never run for as many iterations as it will during
            training. For more information on this parameter, please see `darts.utils.data.ShiftedDataset`
        probabilistic
            Boolean value indicating whether forecasts should be probability distributions
            instead of point predictions. For now, only the Gaussian distribution is supported.
        random_state
            Control the randomness of the weights initialization. Check this
            `link <https://scikit-learn.org/stable/glossary.html#term-random-state>`_ for more details.
        """

        kwargs['input_chunk_length'] = input_chunk_length
        kwargs['output_chunk_length'] = 1
        super().__init__(**kwargs)

        # check we got right model type specified:
        if model not in ['RNN', 'LSTM', 'GRU']:
            raise_if_not(isinstance(model, nn.Module), '{} is not a valid RNN model.\n Please specify "RNN", "LSTM", '
                                                       '"GRU", or give your own PyTorch nn.Module'.format(
                                                        model.__class__.__name__), logger)

        self.rnn_type_or_module = model
        self.dropout = dropout
        self.hidden_dim = hidden_dim
        self.training_length = training_length
        self.is_recurrent = True
        self.is_probabilistic = probabilistic

    def _create_model(self, input_dim: int, output_dim: int) -> torch.nn.Module:
        if self.rnn_type_or_module in ['RNN', 'LSTM', 'GRU']:
            model = _RNNModule(name=self.rnn_type_or_module,
                               input_size=input_dim,
                               target_size=output_dim,
                               hidden_dim=self.hidden_dim,
                               dropout=self.dropout,
                               probabilistic=self.is_probabilistic)
        else:
            model = self.rnn_type_or_module
        return model

    def _build_train_dataset(self,
                             target: Sequence[TimeSeries],
                             covariates: Optional[Sequence[TimeSeries]]) -> ShiftedDataset:
        return ShiftedDataset(target_series=target,
                              covariates=covariates,
                              length=self.training_length,
                              shift_covariates=True,
                              shift=1)

    def _produce_train_output(self, data):
        return self.model(data)[0]

    def _compute_loss(self, output, target):
        if self.is_probabilistic:
            gaussian_loss = nn.GaussianNLLLoss(reduction='sum')
            output_means, output_vars = self._means_and_vars_from_output(output)
            return gaussian_loss(output_means, target, output_vars)
        else:
            return super()._compute_loss(output, target)

    def _produce_predict_output(self, input, last_hidden_state=None):
        if self.is_probabilistic:
            output, hidden = self.model(input, last_hidden_state)
            output_means, output_vars = self._means_and_vars_from_output(output)
            return torch.normal(output_means, output_vars), hidden
        else:
            return self.model(input, last_hidden_state)

    def _means_and_vars_from_output(self, output):
        softplus_activation = nn.Softplus()
        output_means = output[:, :, :self.model.target_size]
        output_vars = softplus_activation(output[:, :, self.model.target_size:])
        #print(output_means.shape, output_vars.shape)
        return output_means, output_vars
