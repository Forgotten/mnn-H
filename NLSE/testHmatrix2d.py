"""
  code for MNN-H 2d.
  reference:
  Y Fan, L Lin, L Ying, L Zepeda-Núnez, A multiscale neural network based on hierarchical matrices,
  arXiv preprint arXiv:1807.01883

  written by Yuwei Fan (ywfan@stanford.edu)
"""
# ------------------ keras ----------------
from keras.models import Model
# layers
from keras.layers import Input, Conv2D
from keras.layers import Add, Lambda

from keras import backend as K
from keras import regularizers, optimizers
from keras.engine.topology import Layer
from keras.constraints import non_neg
from keras.utils import np_utils
# from keras.utils import plot_model
from keras.callbacks import LambdaCallback, ReduceLROnPlateau

import os
import timeit
import argparse
import h5py
import numpy as np
import math, random

K.set_floatx('float32')

parser = argparse.ArgumentParser(description='NLSE - MNN-H 2d')
parser.add_argument('--epoch', type=int, default=4000, metavar='N',
                    help='input number of epochs for training (default: %(default)s)')
parser.add_argument('--input-prefix', type=str, default='nlse2d2', metavar='N',
                    help='prefix of input data filename (default: %(default)s)')
parser.add_argument('--alpha', type=int, default=6, metavar='N',
                    help='number of channels for training (default: %(default)s)')
parser.add_argument('--k-grid', type=int, default=5, metavar='N',
                    help='number of grids (L+1, N=2^L*m) (default: %(default)s)')
parser.add_argument('--n-cnn', type=int, default=5, metavar='N',
                    help='number layer of CNNs (default: %(default)s)')
parser.add_argument('--lr', type=float, default=0.001, metavar='LR',
                    help='learning rate (default: %(default)s)')
parser.add_argument('--batch-size', type=int, default=0, metavar='N',
                    help='batch size (default: #train samples/100)')
parser.add_argument('--verbose', type=int, default=2, metavar='N',
                    help='verbose (default: %(default)s)')
parser.add_argument('--output-suffix', type=str, default='None', metavar='N',
                    help='suffix output filename(default: )')
parser.add_argument('--percent', type=float, default=2./3., metavar='precent',
                    help='percentage of number of total data(default: %(default)s)')
args = parser.parse_args()
# setup: parameters
N_epochs = args.epoch
alpha = args.alpha
k_multigrid = args.k_grid
N_cnn = args.n_cnn
lr = args.lr

Nsamples = 30000

best_err_train = 1e-2
best_err_test = 1e-2
best_err_T_train = 10
best_err_T_test = 10
best_err_train_max = 10
best_err_test_max = 10

# preparation for output
data_path = 'data2d/'
log_path = 'logs2d/'
if not os.path.exists(log_path):
    os.mkdir(log_path)
outputfilename = log_path + 't2dHL' + str(k_multigrid) + 'Nc' + str(N_cnn) + 'Al' + str(alpha);
if(args.output_suffix == 'None'):
    outputfilename += str(os.getpid()) + '.txt'
else:
    outputfilename += args.output_suffix + '.txt'
os = open(outputfilename, "w+")

def output(obj):
    print(obj)
    os.write(str(obj)+'\n')
def outputnewline():
    os.write('\n')
    os.flush()

filenameIpt = data_path + 'Input_'  + args.input_prefix + '.h5'
filenameOpt = data_path + 'Output_' + args.input_prefix + '.h5'

print('Reading data...')
fInput = h5py.File(filenameIpt,'r')
InputArray = fInput['Input'][:,:,0:Nsamples]

fOutput = h5py.File(filenameOpt,'r')
OutputArray = fOutput['Output'][:,:,0:Nsamples]
print('Reading data finished')

InputArray = np.transpose(InputArray, (2,1,0))
OutputArray = np.transpose(OutputArray, (2,1,0))
print(InputArray.shape)

assert InputArray.shape[0] == Nsamples
Nx = InputArray.shape[1]
Ny = InputArray.shape[2]

output(args)
outputnewline()
output('Input data filename     = %s' % filenameIpt)
output('Output data filename    = %s' % filenameOpt)
output("(Nx, Ny)                = (%d, %d)" % (Nx, Ny))
output("Nsamples                = %d" % Nsamples)
outputnewline()

assert OutputArray.shape[0] == Nsamples
assert OutputArray.shape[1] == Nx
assert OutputArray.shape[2] == Ny

n_input = (Nx, Ny)
n_output = (Nx, Ny)

# train data
n_train = int(Nsamples * args.percent)
n_train = min(n_train, 30000)
n_test = Nsamples - n_train
n_test = min(n_test, max(n_train, 5000))
if args.batch_size == 0:
    BATCH_SIZE = n_train // 100
else:
    BATCH_SIZE = args.batch_size

# pre-treat the data
mean_out = np.mean(OutputArray[0:n_train, :, :])
mean_in  = np.mean(InputArray[0:n_train, :, :])
output("mean of input / output is %.6f\t %.6f" % (mean_in, mean_out))
InputArray /= mean_in * 2
InputArray -= 0.5
OutputArray -= mean_out

X_train = InputArray[0:n_train, :, :]
Y_train = OutputArray[0:n_train, :, :]
X_test  = InputArray[n_train:(n_train+n_test), :, :]
Y_test  = OutputArray[n_train:(n_train+n_test), :, :]

output("[n_input, n_output] = [(%d,%d),  (%d,%d)]" % (n_input[0], n_input[1], n_output[0], n_output[1]))
output("[n_train, n_test]   = [%d, %d]" % (n_train, n_test))

X_train = np.reshape(X_train, [X_train.shape[0], X_train.shape[1], X_train.shape[2], 1])
X_test  = np.reshape(X_test,  [X_test.shape[0],  X_test.shape[1],  X_test.shape[2],  1])

# parameters
m = Nx // (2**(k_multigrid - 1))
output('m = %d' % m)

# functions
#channels last, i.e. x.shape = [batch_size, nx, ny, n_channels]
def padding2d(x, size_x, size_y):
    wx = size_x // 2
    wy = size_y // 2
    nx = x.shape[1]
    ny = x.shape[2]
    # x direction
    y = K.concatenate([x[:,nx-wx:nx,:,:], x, x[:,0:wx,:,:]], axis=1)
    # y direction
    z = K.concatenate([y[:,:, ny-wy:ny,:], y, y[:,:,0:wy,:]], axis=2)
    return z

def matrix2tensor(x, w):
    ns = x.shape[0]
    nx = int(x.shape[1])
    ny = int(x.shape[2])
    nw = int(x.shape[3])
    assert nw == 1
    assert nx%w == 0
    assert ny%w == 0
    y = K.reshape(x, (-1, nx//w, w, nx//w, w))
    z = K.permute_dimensions(y, (0,1,3,2,4))
    return K.reshape(z, (-1, nx//w, ny//w, w**2))

def tensor2matrix(x, w):
    ns = x.shape[0]
    nx = int(x.shape[1])
    ny = int(x.shape[2])
    w2 = int(x.shape[3])
    assert w2 == w**2
    y = K.reshape(x, (-1, nx, ny, w, w))
    z = K.permute_dimensions(y, (0, 1, 3, 2, 4))
    return K.reshape(z, (-1, nx*w, ny*w))

# test
def test_data(X, Y, string):
    Yhat = model.predict(X)
    dY = Yhat - Y
    errs = np.linalg.norm(dY, axis=(1,2)) / np.linalg.norm(Y+mean_out, axis=(1,2))
    output("max/ave error of %s data:\t %.1e %.1e" % (string, np.amax(errs), np.mean(errs)))
    return errs

flag = True
def checkresult(epoch, step):
    global best_err_train, best_err_test, best_err_train_max, best_err_test_max, flag, best_err_T_train, best_err_T_test
    t1 = timeit.default_timer()
    if((epoch+1)%step == 0):
        err_train = test_data(X_train, Y_train, 'train')
        err_test  = test_data(X_test, Y_test, 'test')
        if(best_err_train > np.mean(err_train)):
            best_err_train = np.mean(err_train)
            best_err_test = np.mean(err_test)
            best_err_train_max = np.amax(err_train)
            best_err_test_max = np.amax(err_test)
            best_err_T_train = np.var(err_train)
            best_err_T_test = np.var(err_test)
        t2 = timeit.default_timer()
        if(flag):
          output("runtime of checkresult = %.2f secs" % (t2-t1))
          flag = False
        output('best train and test error = %.1e, %.1e,\t fit time    = %.1f secs' % (best_err_train, best_err_test, (t2 - start)))
        output('best train and test error var, max = %.1e, %.1e, %.1e, %.1e' % (best_err_T_train, best_err_train_max, best_err_T_test, best_err_test_max))
        outputnewline()

def outputvec(vec, string):
    os.write(string+'\n')
    for i in range(0, vec.shape[0]):
        os.write("%.6e\n" % vec[i])

Ipt = Input(shape=(n_input[0], n_input[1], 1))

u_list = []
for k in range(0, k_multigrid-2):
    w = m * 2**(k_multigrid-k-3)
    #restriction
    Vv = Conv2D(alpha, (w,w), strides=(w,w), activation='linear')(Ipt)
    #kernel
    MVv = Vv
    if(k==0):
        for i in range(0,N_cnn):
          MVv = Lambda(lambda x: padding2d(x, 5, 5))(MVv)
          MVv = Conv2D(alpha, (5,5), activation='relu')(MVv)
    else:
        for i in range(0,N_cnn):
          MVv = Lambda(lambda x: padding2d(x, 7, 7))(MVv)
          MVv = Conv2D(alpha, (7,7), activation='relu')(MVv)
    #interpolation
    u_l = Conv2D(w**2, (1,1), activation='linear')(MVv)
    u_l = Lambda(lambda x: tensor2matrix(x, w))(u_l)
    u_list.append(u_l)

# adjacent
u_ad = Lambda(lambda x: matrix2tensor(x, w))(Ipt)
for i in range(0, N_cnn-1):
    u_ad = Lambda(lambda x: padding2d(x, 3, 3))(u_ad)
    u_ad = Conv2D(m**2, (3, 3), activation='relu')(u_ad)

u_ad = Lambda(lambda x: padding2d(x, 3, 3))(u_ad)
u_ad = Conv2D(m**2, (3, 3), activation='linear')(u_ad)
u_ad = Lambda(lambda x: tensor2matrix(x, m))(u_ad )
u_list.append(u_ad)

Opt = Add()(u_list)

# model
model = Model(inputs=Ipt, outputs=Opt)
model.compile(loss='mean_squared_error', optimizer='Nadam')
model.optimizer.schedule_decay = (0.004)
output('number of params      = %d' % model.count_params())
outputnewline()
model.summary()

start = timeit.default_timer()
RelativeErrorCallback = LambdaCallback(
        on_epoch_end=lambda epoch, logs: checkresult(epoch, 10))
# ReduceLR = ReduceLROnPlateau(monitor='val_loss', factor=0.5, patience=10,
#         verbose=1, mode='auto', cooldown=0, min_lr=1e-6)
model.optimizer.lr = (lr)
model.fit(X_train, Y_train, batch_size=BATCH_SIZE, epochs=N_epochs, verbose=args.verbose,
        callbacks=[RelativeErrorCallback])

checkresult(1,1)
err_train = test_data(X_train, Y_train, 'train')
err_test  = test_data(X_test, Y_test, 'test')
outputvec(err_train, 'Error for train data')
outputvec(err_test,  'Error for test data')

os.close()

log_os = open('trainresult2dH.txt', "a")
log_os.write('%s\t%d\t%d\t%d\t' % (args.input_prefix, alpha, k_multigrid, N_cnn))
log_os.write('%d\t%d\t%d\t%d\t' % (BATCH_SIZE, n_train, n_test, model.count_params()))
log_os.write('%.3e\t%.3e\t' % (best_err_train, best_err_test))
log_os.write('%.3e\t%.3e\t%.3e\t%.3e\t' % (best_err_train_max, best_err_test_max, best_err_T_train, best_err_T_test))
log_os.write('\n')
log_os.close()
