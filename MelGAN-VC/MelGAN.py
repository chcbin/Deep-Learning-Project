#Imports

from __future__ import print_function, division
from glob import glob
import scipy
import soundfile as sf
import matplotlib.pyplot as plt
from IPython.display import clear_output
import datetime
import numpy as np
import random
import matplotlib.pyplot as plt
import collections
from PIL import Image
import imageio
import librosa
import librosa.display
from librosa.feature import melspectrogram
import os
import time
import IPython

import torch 
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

from tensordot_pytorch import tensordot_pytorch

#Hyperparameters

hop=192               #hop size (window size = 6*hop)
sr=16000              #sampling rate
min_level_db=-100     #reference values to normalize data
ref_level_db=20

shape=24              #length of time axis of split specrograms to feed to generator            
vec_len=128           #length of vector generated by siamese vector
bs = 16               #batch size
delta = 2.            #constant for siamese loss

#There seems to be a problem with Tensorflow STFT, so we'll be using pytorch to handle offline mel-spectrogram generation and waveform reconstruction
#For waveform reconstruction, a gradient-based method is used:

''' Decorsière, Rémi, Peter L. Søndergaard, Ewen N. MacDonald, and Torsten Dau. 
"Inversion of auditory spectrograms, traditional spectrograms, and other envelope representations." 
IEEE/ACM Transactions on Audio, Speech, and Language Processing 23, no. 1 (2014): 46-56.'''

#ORIGINAL CODE FROM https://github.com/yoyololicon/spectrogram-inversion

import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import partial
import math
import heapq
import torchaudio
from torchaudio.transforms import MelScale, Spectrogram

#uncomment if you have a gpu
# torch.set_default_tensor_type('torch.cuda.FloatTensor')

specobj = Spectrogram(n_fft=6*hop, win_length=6*hop, hop_length=hop, pad=0, power=2, normalized=True)
specfunc = specobj.forward
melobj = MelScale(n_mels=hop, sample_rate=sr, f_min=0.)
melfunc = melobj.forward

def melspecfunc(waveform):
    specgram = specfunc(waveform)
    mel_specgram = melfunc(specgram)
    return mel_specgram

def spectral_convergence(input, target):
    return 20 * ((input - target).norm().log10() - target.norm().log10())

def GRAD(spec, transform_fn, samples=None, init_x0=None, maxiter=1000, tol=1e-6, verbose=1, evaiter=10, lr=0.003):

    spec = torch.Tensor(spec)
    samples = (spec.shape[-1]*hop)-hop

    if init_x0 is None:
        init_x0 = spec.new_empty((1,samples)).normal_(std=1e-6)
    x = nn.Parameter(init_x0)
    T = spec

    criterion = nn.L1Loss()
    optimizer = torch.optim.Adam([x], lr=lr)

    bar_dict = {}
    metric_func = spectral_convergence
    bar_dict['spectral_convergence'] = 0
    metric = 'spectral_convergence'

    init_loss = None
    with tqdm(total=maxiter, disable=not verbose) as pbar:
        for i in range(maxiter):
            optimizer.zero_grad()
            V = transform_fn(x)
            loss = criterion(V, T)
            loss.backward()
            optimizer.step()
            lr = lr*0.9999
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr

            if i % evaiter == evaiter - 1:
                with torch.no_grad():
                    V = transform_fn(x)
                    bar_dict[metric] = metric_func(V, spec).item()
                    l2_loss = criterion(V, spec).item()
                    pbar.set_postfix(**bar_dict, loss=l2_loss)
                    pbar.update(evaiter)

    return x.detach().view(-1).cpu()

def normalize(S):
    return np.clip((((S - min_level_db) / -min_level_db)*2.)-1., -1, 1)

def denormalize(S):
    return (((np.clip(S, -1, 1)+1.)/2.) * -min_level_db) + min_level_db

def prep(wv,hop=192):
    S = np.array(torch.squeeze(melspecfunc(torch.Tensor(wv).view(1,-1))).detach().cpu())
    S = librosa.power_to_db(S)-ref_level_db
    return normalize(S)

def deprep(S):
    S = denormalize(S)+ref_level_db
    S = librosa.db_to_power(S)
    wv = GRAD(np.expand_dims(S,0), melspecfunc, maxiter=2000, evaiter=10, tol=1e-8)
    return np.array(np.squeeze(wv))

#Helper functions

#Generate spectrograms from waveform array
def tospec(data):
    print("DATA LEN:",len(data))
    specs=np.empty(len(data), dtype=object)
    for i in range(len(data)):
        x = data[i]
        S=prep(x)
        S = np.array(S, dtype=np.float32)
        specs[i]=np.expand_dims(S, -1)
    return specs

#Generate multiple spectrograms with a determined length from single wav file
def tospeclong(path, length=4*16000):
    x, sr = librosa.load(path,sr=16000)
    x,_ = librosa.effects.trim(x)
    loudls = librosa.effects.split(x, top_db=50)
    xls = np.array([])
    for interv in loudls:
        xls = np.concatenate((xls,x[interv[0]:interv[1]]))
    x = xls
    num = x.shape[0]//length
    specs=np.empty(num, dtype=object)
    for i in range(num-1):
        a = x[i*length:(i+1)*length]
        S = prep(a)
        S = np.array(S, dtype=np.float32)
        try:
            sh = S.shape
            specs[i]=S
        except AttributeError:
            print('spectrogram failed')
    return specs

#Waveform array from path of folder containing wav files
def audio_array(path):
    ls = glob(f'{path}/*.wav')
    adata = []
    for i in range(len(ls)):
        torchaudio.load(ls[i])
        x,sr = torchaudio.load(ls[i])
        # x, sr = tf.audio.decode_wav(tf.io.read_file(ls[i]), 1)
        x = np.array(x, dtype=np.float32)
        adata.append(x)
    return adata

#Concatenate spectrograms in array along the time axis
def testass(a):
    but=False
    con = np.array([])
    nim = a.shape[0]
    for i in range(nim):
        im = a[i]
        im = np.squeeze(im)
        if not but:
            con=im
            but=True
        else:
            con = np.concatenate((con,im), axis=1)
    return np.squeeze(con)

#Split spectrograms in chunks with equal size
def splitcut(data):
    ls = []
    mini = 0
    minifinal = 10*shape    #max spectrogram length
    for i in range(data.shape[0]-1):
        if data[i].shape[1]<=data[i+1].shape[1]:
            mini = data[i].shape[1]
        else:
            mini = data[i+1].shape[1]
        if mini>=3*shape and mini<minifinal:
            minifinal = mini
    for i in range(data.shape[0]):
        x = data[i]
        if x.shape[1]>=3*shape:
            for n in range(x.shape[1]//minifinal):
                ls.append(x[:,n*minifinal:n*minifinal+minifinal,:])
            ls.append(x[:,-minifinal:,:])
    return np.array(ls)

#Generating Mel-Spectrogram dataset (Uncomment where needed)
#adata: source spectrograms
#bdata: target spectrograms

#MALE1
awv = audio_array('./cmu_us_clb_arctic/wav')   #get waveform array from folder containing wav files
aspec = tospec(awv)         #get spectrogram array
print("aspec shape: ", aspec.shape)
adata = splitcut(aspec)     #split spectrogams to fixed length
print("adata shape: ", adata.shape)
#FEMALE1
bwv = audio_array('./cmu_us_bdl_arctic/wav')
bspec = tospec(bwv)
bdata = splitcut(bspec)

# #MALE2
# awv = audio_array('../content/cmu_us_rms_arctic/wav')
# aspec = tospec(awv)
# adata = splitcut(aspec)
# #FEMALE2
# bwv = audio_array('../content/cmu_us_slt_arctic/wav')
# bspec = tospec(bwv)
# bdata = splitcut(bspec)

#JAZZ MUSIC
# awv = audio_array('../content/genres/jazz')
# aspec = tospec(awv)
# adata = splitcut(aspec)
#CLASSICAL MUSIC
# bwv = audio_array('../content/genres/classical')
# bspec = tospec(bwv)
# bdata = splitcut(bspec)

class AudioDataset(Dataset):
    def __init__(self, data, hop, shape):
        self.data = torch.Tensor(data).permute(0,3,1,2)
        self.hop = hop 
        self.shape = shape
        
    def __getitem__(self, idx):
        return self.data[idx,:,:,:3*self.shape]
        
    def __len__(self):
        return len(self.data)

a_dataset = AudioDataset(adata, hop=hop, shape=shape)
a_loader = DataLoader(dataset=a_dataset,batch_size=16,shuffle=True)

b_dataset = AudioDataset(bdata, hop=hop, shape=shape)
b_loader = DataLoader(dataset=b_dataset,batch_size=16,shuffle=True)
    
class ConvSN2D(nn.Conv2d):
    def __init__(self, in_channels, filters, kernel_size, strides, padding='same', power_iterations=1):
        super(ConvSN2D, self).__init__(in_channels=in_channels, out_channels=filters, kernel_size=kernel_size, stride=strides, padding=padding)
        self.power_iterations = power_iterations
        self.strides = strides
        self.padding = padding
        self.filters = filters
        self.kernel_size = kernel_size
        
        self.u = torch.nn.Parameter(data=torch.zeros((1,self.weight.shape[-1]))
                                         ,requires_grad=False)
        
        self.u.data.uniform_(0, 1)
    
    def compute_spectral_norm(self, W, new_u, W_shape):
        for _ in range(self.power_iterations):
            new_v = F.normalize(torch.matmul(new_u, torch.transpose(W,0,1)), p=2)
            new_u = F.normalize(torch.matmul(new_v, W), p=2)
            # new_v = l2normalize(torch.matmul(new_u, torch.transpose(W)))
            # new_u = l2normalize(torch.matmul(new_v, W))
            
        sigma = torch.matmul(W, torch.transpose(new_u,0,1))
        W_bar = W/sigma

        self.u = torch.nn.Parameter(data=new_u)
        W_bar = W_bar.reshape(W_shape)

        return W_bar
    
    def forward(self, inputs):
        W_shape = self.weight.shape
        W_reshaped = self.weight.reshape((-1, W_shape[-1]))
        new_kernel = self.compute_spectral_norm(W_reshaped, self.u, W_shape)
        
        if self.padding == 'same':
            stride_h, stride_w = self.strides if isinstance(self.strides, tuple) else [self.strides, self.strides]
            pad_h = ((inputs.shape[2]-1)*stride_h - inputs.shape[2] + self.kernel_size[0]) // 2
            pad_w = ((inputs.shape[3]-1)*stride_w - inputs.shape[3] + self.kernel_size[1]) // 2
        else:
            pad_h, pad_w = 0, 0

        outputs = F.conv2d(inputs, new_kernel, stride=self.strides, padding=(pad_h, pad_w))

        # CODE TO ADD BIAS AND ACTIVATION FN HERE

        return outputs
    
class ConvSN2DTranspose(nn.ConvTranspose2d):
    def __init__(self, in_channels, filters, kernel_size, power_iterations=1, strides=2, padding='valid'):
        super(ConvSN2DTranspose, self).__init__(in_channels=in_channels, out_channels=filters, kernel_size=kernel_size, stride=strides, padding=padding)
        self.power_iterations = power_iterations
        self.strides = strides
        self.filters = filters
        self.kernel_size = kernel_size
        self.padding=padding
        
        self.u = torch.nn.Parameter(data=torch.zeros((1,self.weight.shape[-1]))
                                         ,requires_grad=False)
        
        self.u.data.uniform_(0, 1)
    
    def compute_spectral_norm(self, W, new_u, W_shape):
        for _ in range(self.power_iterations):
            new_v = F.normalize(torch.matmul(new_u, torch.transpose(W,0,1)), p=2)
            new_u = F.normalize(torch.matmul(new_v, W), p=2)
            
        sigma = torch.matmul(W, torch.transpose(new_u,0,1))
        W_bar = W/sigma

        self.u = torch.nn.Parameter(data=new_u)
        W_bar = W_bar.reshape(W_shape)

        return W_bar
    
    def forward(self, inputs):

        W_shape = self.weight.shape
        W_reshaped = self.weight.reshape((-1, W_shape[-1]))
        new_kernel = self.compute_spectral_norm(W_reshaped, self.u, W_shape)
        
        batch_size = inputs.shape[0]
        height, width = inputs.shape[2], inputs.shape[3]
        
        kernel_h, kernel_w = self.weight.shape[-2:]

        if self.padding == 'same':
            stride_h, stride_w = self.strides if isinstance(self.strides, tuple) else [self.strides, self.strides]
            pad_h = ((inputs.shape[2]-1)*stride_h - 192 + self.kernel_size[0]) // 2
            pad_w = ((inputs.shape[3]-1)*stride_w - 24 + self.kernel_size[1]) // 2
            #Here we are very cheekily forcing output shape...
        else:
            pad_h, pad_w = 0, 0

        outputs = F.conv_transpose2d(
            inputs,
            new_kernel,
            None,
            stride=self.strides,
            padding=(pad_h,pad_w))

        # CODE FOR BIAS AND ACTIVATION FN HERE  
        return outputs
    
class DenseSN(nn.Linear):
    def __init__(self, input_shape):
        super(DenseSN, self).__init__(in_features=input_shape, out_features=1)
        
        self.u = torch.nn.Parameter(data=torch.zeros((1,self.weight.shape[-1]))
                                         ,requires_grad=False)
        
        self.u.data.uniform_(0, 1)
    
    def compute_spectral_norm(self, W, new_u, W_shape):
        new_v = F.normalize(torch.matmul(new_u, torch.transpose(W,0,1)), p=2)
        new_u = F.normalize(torch.matmul(new_v, W), p=2)
            
        sigma = torch.matmul(W, torch.transpose(new_u,0,1))
        W_bar = W/sigma

        self.u = torch.nn.Parameter(data=new_u)
        W_bar = W_bar.reshape(W_shape)

        return W_bar
    
    def forward(self, inputs):
        W_shape = self.weight.shape
        W_reshaped = self.weight.reshape((-1, W_shape[-1]))
        new_kernel = self.compute_spectral_norm(W_reshaped, self.u, W_shape)
        
        rank = len(inputs.shape)
        
        if rank > 2:
            #Thanks to deanmark on GitHub for pytorch tensordot function
            outputs = tensordot_pytorch(inputs, new_kernel, [[rank-1],[0]])
        else:
            outputs = torch.matmul(inputs, torch.transpose(new_kernel,0,1))
            
        # CODE FOR BIAS AND ACTIVATION FN HERE 
        return outputs

#Extract function: splitting spectrograms
def extract_image(im):
    shape = im.shape
    height = shape[2]
    width = shape[3]

    im1 = im[:,:,:, 0:(width - (2*width//3))]
    im2 = im[:,:,:, width//3:(width-(width//3))]
    im3 = im[:,:,:,(2*width//3):width]

    return im1,im2,im3

#Assemble function: concatenating spectrograms
def assemble_image(lsim):
    im1,im2,im3 = lsim
    imh = torch.cat((im1,im2,im3),dim=3)
    return imh

class Generator(nn.Module):
    def __init__(self,input_shape):
        super(Generator, self).__init__()
        
        h, w, c = input_shape

        self.leaky = nn.LeakyReLU(0.2, inplace=True)
        self.batchnorm = nn.BatchNorm2d(num_features=256)
        
        #downscaling
        #self.g0 = nn.ConstantPad2d((0,1), 0)
        self.g1 = ConvSN2D(in_channels=c, filters=256, kernel_size=(h,3), strides=1, padding='valid')
        self.g2 = ConvSN2D(in_channels=256, filters=256, kernel_size=(1,9), strides=(1,2))
        self.g3 = ConvSN2D(in_channels=256, filters=256, kernel_size=(1,7), strides=(1,2))
        
        #upscaling
        self.g4 = ConvSN2D(in_channels=256, filters=256, kernel_size=(1,7), strides=(1,1))
        self.g5 = ConvSN2D(in_channels=256, filters=256, kernel_size=(1,9), strides=(1,1))
        
        self.g6 = ConvSN2DTranspose(in_channels=256, filters=1, kernel_size=(h,2), strides=(1,1), padding='same')
    
    def forward(self, x):
        #NOTE: YET TO IMPLEMENT BATCH NORM, ACTIVATION FUNCTIONS, RELU ETC
        #downscaling
        #x = self.g0(x)
        x1 = self.g1(x)
        x1 = self.leaky(self.batchnorm(x1))
        x2 = self.g2(x1)   
        x2 = self.leaky(self.batchnorm(x2))   
        x3 = self.g3(x2)
        x3 = self.leaky(self.batchnorm(x3))
        
        #upscaling
        x4 = F.interpolate(x3, size=(x3.shape[2],x3.shape[3] * 2))
        x = self.g4(x4)
        x = self.leaky(self.batchnorm(x))
        x = torch.cat((x, x3), dim=3)

        x = F.interpolate(x, size=(x.shape[2],x.shape[3] * 2))
        x = self.g5(x)
        x = self.leaky(x)
        x = torch.cat((x,x2),dim=3)

        x = torch.tanh(self.g6(x))

        return x
        
#torch.nn.Conv2d(in_channels, out_channels, kernel_size, stride=1, 
#padding=0, dilation=1, groups=1, bias=True, padding_mode='zeros')
        
class Siamese(nn.Module):
    def __init__(self,input_shape):
        super(Siamese, self).__init__()
        
        h, w, c = input_shape
        self.leaky = nn.LeakyReLU(0.2, inplace=True)
        self.batchnorm = nn.BatchNorm2d(num_features=256)

        self.g1 = nn.Conv2d(in_channels=c, out_channels=256, kernel_size=(h,9), stride=1, padding=0)
        self.g2 = nn.Conv2d(in_channels=256, out_channels=256, kernel_size=(1,9), stride=(1,2))
        self.g3 = nn.Conv2d(in_channels=256, out_channels=256, kernel_size=(1,7), stride=(1,2))
        self.g4 = nn.Linear(1536, 128)
            
    def forward(self, x):
        #We have to define layers on the fly in order to ensure 'same' padding

        x = self.g1(x)
        x = self.leaky(self.batchnorm(x))

        #New stride = (1,2)
        #New kernel = (1,9)
        pad_h = ((x.shape[2]-1)*1 - x.shape[2] + 1) // 2
        pad_w = ((x.shape[3]-1)*2 - x.shape[3] + 9) // 2
        x = F.pad(x, (pad_h, pad_w))
        x = self.g2(x)
        x = self.leaky(self.batchnorm(x))

        #New stride = (1,2)
        #New kernel = (1,7)
        pad_h = ((x.shape[2]-1)*1 - x.shape[2] + 1) // 2
        pad_w = ((x.shape[3]-1)*2 - x.shape[3] + 7) // 2
        x = F.pad(x, (pad_h, pad_w))
        x = self.g3(x)
        x = self.leaky(self.batchnorm(x))

        x = self.g4(x.view(x.shape[0],-1))
        return x
    
class Discriminator(nn.Module):
    def __init__(self,input_shape):
        super(Discriminator, self).__init__()
        
        h, w, c = input_shape
        
        self.leaky = nn.LeakyReLU(0.2, inplace=True)

        self.g1 = ConvSN2D(in_channels=c, filters=512, kernel_size=(h,3), strides=1, padding='valid')
        self.g2 = ConvSN2D(in_channels=512, filters=512, kernel_size=(1,9), strides=(1,2))
        self.g3 = ConvSN2D(in_channels=512, filters=512, kernel_size=(1,7), strides=(1,2))
        self.g4 = DenseSN(input_shape=35328)
        
    def forward(self, x):
        x = self.g1(x)
        x = self.leaky(x)
        x = self.g2(x)
        x = self.leaky(x)
        x = self.g3(x)
        x = self.leaky(x)
        x = self.g4(x.view(x.shape[0],-1))
        return x   

#Generate a random batch to display current training results
def testgena():
    sw = True
    while sw:
        a = np.random.choice(aspec)
        if a.shape[1]//shape!=1:
            sw=False
    dsa = []
    if a.shape[1]//shape>6:
        num=6
    else:
        num=a.shape[1]//shape
    rn = np.random.randint(a.shape[1]-(num*shape))
    for i in range(num):
        im = a[:,rn+(i*shape):rn+(i*shape)+shape]
        im = np.reshape(im, (im.shape[0],im.shape[1],1))
        dsa.append(im)
    return np.array(dsa, dtype=np.float32)

#Show results mid-training
def save_test_image_full(path):
    a = testgena()
    ab = gen(a, training=False)
    ab = testass(ab)
    a = testass(a)
    abwv = deprep(ab)
    awv = deprep(a)
    sf.write(path+'/new_file.wav', abwv, sr)
    IPython.display.display(IPython.display.Audio(np.squeeze(abwv), rate=sr))
    IPython.display.display(IPython.display.Audio(np.squeeze(awv), rate=sr))
    fig, axs = plt.subplots(ncols=2)
    axs[0].imshow(np.flip(a, -2), cmap=None)
    axs[0].axis('off')
    axs[0].set_title('Source')
    axs[1].imshow(np.flip(ab, -2), cmap=None)
    axs[1].axis('off')
    axs[1].set_title('Generated')
    plt.show()

#===========DEFINE LOSS FUNCS =============#

def mae(x,y):
    loss_REC_mean = nn.MSELoss(reduction='mean')
    #return loss_REC_mean(torch.abs(x-y))
    return torch.mean(torch.abs(x - y))
    # return loss_REC_mean(x, y)

def mse(x,y):
    return torch.mean((x-y)**2)

def loss_travel(sa,sab,sa1,sab1):
    #l1 = loss_REC_mean(((sa-sa1) - (sab-sab1))**2)
    #l2 = loss_REC_mean(loss_REC(-(torch.norm(sa-sa1, axis=[-1]) * torch.norm(sab-sab1, axis=[-1])), axis=-1))
    # l1 = torch.mean(((sa-sa1) - (sab-sab1))**2)
    # l2 = torch.mean(torch.sum(-(torch.norm(sa-sa1,2,-1) * torch.norm(sab-sab1,2,-1)), -1))
    #Recreate in PyTorch
    cos = nn.CosineSimilarity(dim=-1, eps=1e-6)
    l1 = cos((sa-sa1), (sab-sab1))
    print("l1 shape :", l1.shape)
    l2 = F.normalize((sa-sa1) - (sab-sab1), p=2).mean(axis=1)
    print("l2 shape: ", l2.shape)
    return l1 + l2

def loss_siamese(sa,sa1):
    logits = torch.sqrt(((sa-sa1)**2).sum(1))
    logits_max = torch.tensor(torch.max((delta - logits), 0),requires_grad=True)
    return torch.pow(logits_max, 2.).mean(axis=-1)

def d_loss_f(fake):
    tensor = torch.tensor(torch.max(1 + fake, 0),requires_grad=True)
    return tensor.mean(-1)

def d_loss_r(real):
    tensor = torch.tensor(torch.max(1 - real, 0),requires_grad=True)
    return tensor.mean(-1)

def g_loss_f(fake):
    tensor = torch.tensor(-fake,requires_grad=True)
    return tensor.mean(-1)

#=======SET UP MODELS AND OPTIMIZERS =======#
gen = Generator((hop,shape,1))
siam = Siamese((hop,shape,1))
critic = Discriminator((hop,3*shape,1))

#Generator loss is a function of 
params = list(gen.parameters()) + list(siam.parameters())
opt_gen = optim.Adam(params, lr=0.0001)

opt_disc = optim.Adam(critic.parameters(), lr=0.0001)

#Set learning rate
def update_lr(lr):
    opt_gen.lr = lr
    opt_disc.lr = lr

#functions to be written here
def train_all(a,b):
    #splitting spectrogram in 3 parts
    aa,aa2,aa3 = extract_image(a) 
    
    bb,bb2,bb3 = extract_image(b)

    gen.zero_grad()
    critic.zero_grad()
    siam.zero_grad()
    
    opt_gen.zero_grad()
    opt_disc.zero_grad()

    #translating A to B
    fab = gen.forward(aa)
    fab2 = gen.forward(aa2)
    fab3 = gen.forward(aa3)
    
    #identity mapping B to B  COMMENT THESE 3 LINES IF THE IDENTITY LOSS TERM IS NOT NEEDED
    #fid = gen.forward(bb)
    #fid2 = gen.forward(bb2)
    #fid3 = gen.forward(bb3)
    
    #concatenate/assemble converted spectrograms
    fabtot = assemble_image([fab,fab2,fab3])

    #feed concatenated spectrograms to critic
    cab = critic.forward(fabtot)
    cb = critic.forward(b)

    #feed 2 pairs (A,G(A)) extracted spectrograms to Siamese
    sab = siam.forward(fab)
    sab2 = siam.forward(fab3)
    sa = siam.forward(aa)
    sa2 = siam.forward(aa3)

    #identity mapping loss
    #loss_id = (mae(bb,fid)+mae(bb2,fid2)+mae(bb3,fid3))/3.      #loss_id = 0. IF THE IDENTITY LOSS TERM IS NOT NEEDED
    #travel loss
    loss_m = loss_travel(sa,sab,sa2,sab2)+loss_siamese(sa,sa2)
    print("Loss m: ", loss_m)
    #get gen and siam loss and bptt
    loss_g = g_loss_f(cab)
    print("Loss g: ", loss_g)
    lossgtot = loss_g+10.*loss_m #+0.5*loss_id #CHANGE LOSS WEIGHTS HERE  (COMMENT OUT +w*loss_id IF THE IDENTITY LOSS TERM IS NOT NEEDED)

    lossgtot.mean().backward()
    opt_gen.step()
    
    #get critic loss and bptt
    loss_dr = d_loss_r(cb)
    loss_df = d_loss_f(cab)
    loss_d = (loss_dr+loss_df)/2.
    # loss_d = torch.autograd.Variable(loss_d, requires_grad = True)
    loss_d.backward()
    opt_disc.step()
    
    return loss_dr,loss_df,loss_g,loss_d

def train_d(a,b):
    opt_disc.zero_grad()
    
    aa,aa2,aa3 = extract_image(a)
    
    #translating A to B
    fab = gen.forward(aa)
    fab2 = gen.forward(aa2)
    fab3 = gen.forward(aa3)
    #concatenate/assemble converted spectrograms
    fabtot = assemble_image([fab,fab2,fab3])

    #feed concatenated spectrograms to critic
    cab = critic.forward(fabtot)
    cb = critic.forward(b)

    #get critic loss and bptt
    loss_dr = d_loss_r(cb)
    loss_df = d_loss_f(cab)
    loss_d = (loss_dr+loss_df)/2.
    # loss_d = torch.autograd.Variable(loss_d, requires_grad = True)
    
    loss_d.backward()
    opt_disc.step()

    return loss_dr,loss_df

def train(epochs, batch_size=16, lr=0.0001, n_save=6, gupt=5):
  
    update_lr(lr)
    df_list = []
    dr_list = []
    g_list = []
    id_list = []
    c = 0
    g = 0
  
    for epoch in range(epochs):
        for batchi,(a,b) in enumerate(zip(a_loader,b_loader)):
            #only train discriminator every gupt'th batch
            if batchi%gupt==0:
                dloss_t,dloss_f,gloss,idloss = train_all(a,b)
            else:
                dloss_t,dloss_f = train_d(a,b)

            df_list.append(dloss_f)
            dr_list.append(dloss_t)
            g_list.append(gloss)
            id_list.append(idloss)
            c += 1
            g += 1

            if batchi%600==0:
                # print(f'[Epoch {epoch}/{epochs}] [Batch {batchi}] [D loss f: {np.mean(df_list[-g:], axis=0)} ', end='')
                # print(f'r: {np.mean(dr_list[-g:], axis=0)}] ', end='')
                # print(f'[G loss: {np.mean(g_list[-g:], axis=0)}] ', end='')
                # print(f'[ID loss: {np.mean(id_list[-g:])}] ', end='')
                # print(f'[LR: {lr}]')
                g = 0
            nbatch=batchi

        print(f'Time/Batch {(time.time()-bef)/nbatch}')
        save_end(epoch,np.mean(g_list[-n_save*c:], axis=0),np.mean(df_list[-n_save*c:], axis=0),np.mean(id_list[-n_save*c:], axis=0),n_save=n_save)
        print(f'Mean D loss: {np.mean(df_list[-c:], axis=0)} Mean G loss: {np.mean(g_list[-c:], axis=0)} Mean ID loss: {np.mean(id_list[-c:], axis=0)}')
        c = 0
        
train(5000, batch_size=bs, lr=0.0002, n_save=1, gupt=3)

#After Training, use these functions to convert data with the generator and save the results

#Assembling generated Spectrogram chunks into final Spectrogram
def specass(a,spec):
    but=False
    con = np.array([])
    nim = a.shape[0]
    for i in range(nim-1):
        im = a[i]
        im = np.squeeze(im)
        if not but:
            con=im
            but=True
        else:
            con = np.concatenate((con,im), axis=1)
    diff = spec.shape[1]-(nim*shape)
    a = np.squeeze(a)
    con = np.concatenate((con,a[-1,:,-diff:]), axis=1)
    return np.squeeze(con)

#Splitting input spectrogram into different chunks to feed to the generator
def chopspec(spec):
    dsa=[]
    for i in range(spec.shape[1]//shape):
        im = spec[:,i*shape:i*shape+shape]
        im = np.reshape(im, (im.shape[0],im.shape[1],1))
        dsa.append(im)
    imlast = spec[:,-shape:]
    imlast = np.reshape(imlast, (imlast.shape[0],imlast.shape[1],1))
    dsa.append(imlast)
    return np.array(dsa, dtype=np.float32)

#Converting from source Spectrogram to target Spectrogram
def towave(spec, name, path='../content/', show=False):
    specarr = chopspec(spec)
    a = specarr
    ab = gen(a, training=False)
    a = specass(a,spec)
    ab = specass(ab,spec)
    awv = deprep(a)
    abwv = deprep(ab)
    pathfin = f'{path}/{name}'
    os.mkdir(pathfin)
    sf.write(pathfin+'/AB.wav', abwv, sr)
    sf.write(pathfin+'/A.wav', awv, sr)
    IPython.display.display(IPython.display.Audio(np.squeeze(abwv), rate=sr))
    IPython.display.display(IPython.display.Audio(np.squeeze(awv), rate=sr))
    if show:
        fig, axs = plt.subplots(ncols=2)
        axs[0].imshow(np.flip(a, -2), cmap=None)
        axs[0].axis('off')
        axs[0].set_title('Source')
        axs[1].imshow(np.flip(ab, -2), cmap=None)
        axs[1].axis('off')
        axs[1].set_title('Generated')
        plt.show()
    return abwv

#Wav to wav conversion

wv, sr = librosa.load(librosa.util.example_audio_file(), sr=16000)  #Load waveform
print(wv.shape)
speca = prep(wv)                                                    #Waveform to Spectrogram

plt.figure(figsize=(50,1))                                          #Show Spectrogram
plt.imshow(np.flip(speca, axis=0), cmap=None)
plt.axis('off')
plt.show()

abwv = towave(speca, name='FILENAME1', path='../content/')           #Convert and save wav