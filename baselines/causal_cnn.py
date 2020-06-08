
import torch
import numpy as np
import numpy
import os
import random
import pickle
import matplotlib.pyplot as plt
import seaborn as sns; sns.set()
from sklearn.model_selection import KFold

from tcl.models import RnnEncoder, WFEncoder
from tcl.utils import plot_distribution, model_distribution
from tcl.evaluations import ClassificationPerformanceExperiment, WFClassificationExperiment

class TripletLoss(torch.nn.modules.loss._Loss):
    """
    Triplet loss for representations of time series. Optimized for training
    sets where all time series have the same length.
    Takes as input a tensor as the chosen batch to compute the loss,
    a PyTorch module as the encoder, a 3D tensor (`B`, `C`, `L`) containing
    the training set, where `B` is the batch size, `C` is the number of
    channels and `L` is the length of the time series, as well as a boolean
    which, if True, enables to save GPU memory by propagating gradients after
    each loss term, instead of doing it after computing the whole loss.
    The triplets are chosen in the following manner. First the size of the
    positive and negative samples are randomly chosen in the range of lengths
    of time series in the dataset. The size of the anchor time series is
    randomly chosen with the same length upper bound but the the length of the
    positive samples as lower bound. An anchor of this length is then chosen
    randomly in the given time series of the train set, and positive samples
    are randomly chosen among subseries of the anchor. Finally, negative
    samples of the chosen length are randomly chosen in random time series of
    the train set.
    @param compared_length Maximum length of randomly chosen time series. If
           None, this parameter is ignored.
    @param nb_random_samples Number of negative samples per batch example.
    @param negative_penalty Multiplicative coefficient for the negative sample
           loss.
    """
    def __init__(self, compared_length, nb_random_samples, negative_penalty):
        super(TripletLoss, self).__init__()
        self.compared_length = compared_length
        if self.compared_length is None:
            self.compared_length = np.inf
        self.nb_random_samples = nb_random_samples
        self.negative_penalty = negative_penalty

    def forward(self, batch, encoder, train, save_memory=False):
        batch=batch.to(device)
        train=train.to(device)
        encoder = encoder.to(device)
        batch_size = batch.size(0)
        train_size = train.size(0)
        length = min(self.compared_length, train.size(2))

        # For each batch element, we pick nb_random_samples possible random
        # time series in the training set (choice of batches from where the
        # negative examples will be sampled)
        samples = np.random.choice(
            train_size, size=(self.nb_random_samples, batch_size)
        )
        samples = torch.LongTensor(samples)

        # Choice of length of positive and negative samples
        length_pos_neg = self.compared_length
        # length_pos_neg = np.random.randint(1, high=length + 1)


        # We choose for each batch example a random interval in the time
        # series, which is the 'anchor'
        random_length = self.compared_length
        # random_length = np.random.randint(
        #     length_pos_neg, high=length + 1
        # )  # Length of anchors

        beginning_batches = np.random.randint(
            0, high=length - random_length + 1, size=batch_size
        )  # Start of anchors

        # The positive samples are chosen at random in the chosen anchors
        beginning_samples_pos = np.random.randint(
            0, high=random_length + 1, size=batch_size
            # 0, high=random_length - length_pos_neg + 1, size=batch_size
        )  # Start of positive samples in the anchors
        # Start of positive samples in the batch examples
        beginning_positive = beginning_batches + beginning_samples_pos
        # End of positive samples in the batch examples
        end_positive = beginning_positive + length_pos_neg + np.random.randint(0,self.compared_length)

        # We randomly choose nb_random_samples potential negative samples for
        # each batch example
        beginning_samples_neg = np.random.randint(
            0, high=length - length_pos_neg + 1,
            size=(self.nb_random_samples, batch_size)
        )

        # print('Actual ...............', torch.cat(
        #     [batch[
        #         j: j + 1, :,
        #         beginning_batches[j]: beginning_batches[j] + random_length
        #     ] for j in range(batch_size)]).shape)

        representation = encoder(torch.cat(
            [batch[
                j: j + 1, :,
                beginning_batches[j]: beginning_batches[j] + random_length
            ] for j in range(batch_size)]).to(device))  # Anchors representations


        # print('Positive ......', torch.cat([batch[
        #                  j: j + 1, :, end_positive[j] - length_pos_neg: end_positive[j]
        #                  ] for j in range(batch_size)]).shape)

        positive_representation = encoder(torch.cat(
            [batch[
                j: j + 1, :, end_positive[j] - length_pos_neg: end_positive[j]
            ] for j in range(batch_size)]
        ))  # Positive samples representations

        size_representation = representation.size(1)
        # Positive loss: -logsigmoid of dot product between anchor and positive
        # representations
        loss = -torch.mean(torch.nn.functional.logsigmoid(torch.bmm(
            representation.view(batch_size, 1, size_representation),
            positive_representation.view(batch_size, size_representation, 1)
        )))

        # If required, backward through the first computed term of the loss and
        # free from the graph everything related to the positive sample
        if save_memory:
            loss.backward(retain_graph=True)
            loss = 0
            del positive_representation
            torch.cuda.empty_cache()

        multiplicative_ratio = self.negative_penalty / self.nb_random_samples
        for i in range(self.nb_random_samples):
            # Negative loss: -logsigmoid of minus the dot product between
            # anchor and negative representations

            # print('Negative .....', torch.cat([train[samples[i, j]: samples[i, j] + 1][
            #         :, :,
            #         beginning_samples_neg[i, j]:
            #         beginning_samples_neg[i, j] + length_pos_neg
            #     ] for j in range(batch_size)]).shape)

            negative_representation = encoder(
                torch.cat([train[samples[i, j]: samples[i, j] + 1][
                    :, :,
                    beginning_samples_neg[i, j]:
                    beginning_samples_neg[i, j] + length_pos_neg
                ] for j in range(batch_size)])
            )
            loss += multiplicative_ratio * -torch.mean(
                torch.nn.functional.logsigmoid(-torch.bmm(
                    representation.view(batch_size, 1, size_representation),
                    negative_representation.view(
                        batch_size, size_representation, 1
                    )
                ))
            )
            # If required, backward through the first computed term of the loss
            # and free from the graph everything related to the negative sample
            # Leaves the last backward pass to the training procedure
            if save_memory and i != self.nb_random_samples - 1:
                loss.backward(retain_graph=True)
                loss = 0
                del negative_representation
                torch.cuda.empty_cache()

        return loss


def epoch_run(data, encoder, device, window_size, optimizer=None, train=True):
    if train:
        encoder.train()
    else:
        encoder.eval()
    encoder = encoder.to(device)
    loss_criterion = TripletLoss(compared_length=window_size, nb_random_samples=10, negative_penalty=1)

    epoch_loss = 0
    acc = 0
    dataset = torch.utils.data.TensorDataset(torch.Tensor(data).to(device), torch.zeros((len(data),1)).to(device))
    data_loader = torch.utils.data.DataLoader(dataset, batch_size=20, shuffle=True, sampler=None, batch_sampler=None,
                    num_workers=0, collate_fn=None, pin_memory=False, drop_last=False)
    i = 0
    for x_batch,y in data_loader:
        loss = loss_criterion(x_batch.to(device), encoder, torch.Tensor(data).to(device))
        epoch_loss += loss.item()
        i += 1
        if train:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
    return epoch_loss/i, acc/i


def learn_encoder(x, encoder, window_size, data, lr=0.001, decay=0, n_epochs=100, device='cpu'):
    # n_train = int(len(x)*0.8)
    # inds = list(range(len(x)))
    # random.shuffle(inds)
    # x = x[inds]

    # cv = 0
    # kf = KFold(n_splits=4)
    # for train_index, test_index in kf.split(x):
    for cv in range(4):
        encoder = WFEncoder(encoding_size=64).to(device)
        params = encoder.parameters()
        optimizer = torch.optim.Adam(params, lr=lr, weight_decay=decay)
        inds = list(range(len(x)))
        random.shuffle(inds)
        x = x[inds]
        n_train = int(0.8*len(x))
        performance = []
        best_loss = np.inf
        train_loss, test_loss = [], []
        best_loss = np.inf
        for epoch in range(n_epochs):
            epoch_loss, acc = epoch_run(x[:n_train], encoder, device, window_size, optimizer=optimizer, train=True)
            # epoch_loss, acc = epoch_run(x[train_index], encoder, device, window_size, optimizer=optimizer, train=True)
            epoch_loss_test, acc_test = epoch_run(x[n_train:], encoder, device, window_size, optimizer=optimizer, train=False)
            print('\nEpoch ', epoch)
            print('Train ===> Loss: ', epoch_loss, '\t Accuracy: ', acc)
            print('Test ===> Loss: ', epoch_loss_test, '\t Accuracy: ', acc_test)
            train_loss.append(epoch_loss)
            test_loss.append(epoch_loss_test)
            if epoch_loss_test<best_loss:
                print('Save new ckpt')
                state = {
                    'epoch': epoch,
                    'encoder_state_dict': encoder.state_dict()
                }
                best_loss = epoch_loss_test
                torch.save(state, './ckpt/%s_trip/checkpoint_%d.pth.tar' %(data, cv))
        plt.figure()
        plt.plot(np.arange(n_epochs), train_loss, label="Train")
        plt.plot(np.arange(n_epochs), test_loss, label="Test")
        plt.title("Loss")
        plt.legend()
        plt.savefig(os.path.join("./plots/%s_trip/loss_%d.pdf"%(data,cv)))


device = 'cuda' if torch.cuda.is_available() else 'cpu'

def main(data):
    if data =='waveform':
        path = './data/waveform_data/processed'
        kf = KFold(n_splits=4)
        encoding_size = 64
        window_size = 2500
        encoder = WFEncoder(encoding_size=64).to(device)
        with open(os.path.join(path, 'x_train.pkl'), 'rb') as f:
            x = pickle.load(f)
        # with open(os.path.join(path, 'state_train.pkl'), 'rb') as f:
        #     y = pickle.load(f)


        T = x.shape[-1]
        x_window = np.concatenate(np.split(x[:, :, :T // 5 * 5], 5, -1), 0)
        learn_encoder(x_window, encoder, window_size, n_epochs=50, lr=1e-5, decay=1e-2, data='waveform')


        # T = x.shape[-1]
        # x_window = np.concatenate(np.split(x[:, :, :T // 5 * 5], 5, -1), 0)
        # y_window = np.concatenate(np.split(y[:, :5 * (T // 5)], 5, -1), 0).astype(int)
        # # y_window = np.array([np.bincount(yy).argmax() for yy in y_window])
        # shiffled_inds = list(range(len(x_window)))
        # random.shuffle(shiffled_inds)
        # x_window = x_window[shiffled_inds]
        # y_window = y_window[shiffled_inds]
        # cv = 0
        # for train_index, test_index in kf.split(x_window):
        #     X_train, X_test = x_window[train_index], x_window[test_index]
        #     y_train, y_test = y_window[train_index], y_window[test_index]
        #     print(X_train.shape, y_train.shape)
        #     learn_encoder(X_train, encoder, window_size, n_epochs=50, lr=1e-5, decay=1e-2, data='waveform', cv=cv)
        #     cv += 1


        # T = x.shape[-1]
        # x = np.concatenate(np.split(x[:,:,:T//20*20], 20, -1), 0)
        # learn_encoder(x, encoder, window_size, n_epochs=50, lr=1e-5, decay=1e-2, data=data)
        # with open(os.path.join(path, 'x_test.pkl'), 'rb') as f:
        #     x_test = pickle.load(f)
        # with open(os.path.join(path, 'state_test.pkl'), 'rb') as f:
        #     y_test = pickle.load(f)
        # T = x_test.shape[-1]
        # # x_test = np.concatenate(np.split(x_test[:,:,:T//50*50], 50, -1), 0)
        # # y_test = np.concatenate(np.split(y_test[:, :T //50 *50], 50, -1), 0)
        # plot_distribution(x_test, y_test, encoder, window_size=window_size, path='%s_trip' % data, device=device, augment=100)
        # model_distribution(None, None, x_test, y_test, encoder, window_size, 'waveform', device)
        # exp = WFClassificationExperiment(window_size=window_size, data='waveform_trip')
        # exp.run(data='waveform_trip', n_epochs=15, lr_e2e=0.001, lr_cls=0.001)


    else:
        path = './data/simulated_data/'
        kf = KFold(n_splits=4)
        window_size = 50
        encoder = RnnEncoder(hidden_size=100, in_channel=3, encoding_size=10, device=device).to(device)
        with open(os.path.join(path, 'x_train.pkl'), 'rb') as f:
            x = pickle.load(f)
        with open(os.path.join(path, 'state_train.pkl'), 'rb') as f:
            y = pickle.load(f)
        cv = 0
        for train_index, test_index in kf.split(x):
            X_train, X_test = x[train_index], x[test_index]
            y_train, y_test = y[train_index], y[test_index]
            print(X_train.shape, y_train.shape)
            learn_encoder(X_train, encoder, window_size, lr=1e-4, decay=0.0001, data=data, n_epochs=100, device=device)
            cv += 1

        # # learn_encoder(x, encoder, window_size, lr=1e-3, decay=0.0001, data=data, n_epochs=200, device=device)
        # with open(os.path.join(path, 'x_test.pkl'), 'rb') as f:
        #     x_test = pickle.load(f)
        # with open(os.path.join(path, 'state_test.pkl'), 'rb') as f:
        #     y_test = pickle.load(f)
        # # plot_distribution(x_test, y_test, encoder, window_size=window_size, path='%s_trip' % data, title='Triplet Loss', device=device)
        # model_distribution(x, y, x_test, y_test, encoder, window_size, 'simulation_trip', device)
        # # exp = ClassificationPerformanceExperiment(path='simulation_trip')
        # # exp.run(data='simulation_trip', n_epochs=70, lr_e2e=0.01, lr_cls=0.001)

if __name__=="__main__":
    data = 'waveform'
    random.seed(1234)
    main(data)