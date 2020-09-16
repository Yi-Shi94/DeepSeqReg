import set_path
import os
import argparse
import functools
print = functools.partial(print,flush=True)
import open3d
import numpy as np
import torch
import torch.optim as optim
from torch.utils.data import DataLoader

import utils
import loss
from models import DeepMapping2D
from dataset_loader import SimulatedPointCloud

torch.backends.cudnn.deterministic = True
print("LSTM")
parser = argparse.ArgumentParser()
parser.add_argument('--name',type=str,default='test',help='experiment name')
parser.add_argument('-e','--n_epochs',type=int,default=1000,help='number of epochs')
parser.add_argument('-b','--batch_size',type=int,default=32,help='batch_size')
parser.add_argument('-l','--loss',type=str,default='bce_ch',help='loss function')
parser.add_argument('-n','--n_samples',type=int,default=19,help='number of sampled unoccupied points along rays')
parser.add_argument('--lr',type=float,default=0.001,help='learning rate')
parser.add_argument('-d','--data_dir',type=str,default='../data/2D/',help='dataset path')
parser.add_argument('-m','--model', type=str, default=None,help='pretrained model name')
parser.add_argument('-i','--init', type=str, default=None,help='init pose')
parser.add_argument('--log_interval',type=int,default=10,help='logging interval of saving results')
parser.add_argument('-s','--seed',type=int,help='random start seed')
parser.add_argument('-k','--conv_size',type=int, help='convsize of latent vector') 
parser.add_argument('--latent_size',type=int, help='latent size of latent vector')
parser.add_argument('--mode',type=str,default='maxpool',help='mode of latent gen')
parser.add_argument('--op',type=str,default='add',help='operation on foward and reverse latent in RNN like method')
parser.add_argument('-g','--gpu',type=str,default='1',help='gpu_index')
opt = parser.parse_args()
torch.manual_seed(opt.seed)

print("SEED",opt.seed)
checkpoint_dir = os.path.join('../results/2D',opt.name)
if not os.path.exists(checkpoint_dir):
    os.makedirs(checkpoint_dir)
utils.save_opt(checkpoint_dir,opt)
os.environ["CUDA_VISIBLE_DEVICES"]=opt.gpu
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(device)


print('loading dataset')
if opt.init is not None:
    init_pose_np = np.load(opt.init)
    init_pose = torch.from_numpy(init_pose_np)
else:
    init_pose = None
    
latent_vecs = []
 
latent_size = opt.latent_size
print("latent_size",latent_size)
instances_per_scene = 256
for i in range(instances_per_scene):
    vec = (torch.ones(latent_size).normal_(0, 0.8).to(device))
    vec.requires_grad = True
    latent_vecs.append(vec)

w = torch.ones(latent_size).normal_(0,0.8).to(device)
w_r = torch.ones(latent_size).normal_(0,0.8).to(device)


dataset = SimulatedPointCloud(opt.data_dir,instances_per_scene,init_pose)
loader = DataLoader(dataset,batch_size=opt.batch_size,shuffle=False)
#print(len(dataset))
loss_fn = eval('loss.'+opt.loss)


print('creating model')
if opt.op=='cat':
     latent_size *= 2 
model = DeepMapping2D(loss_fn=loss_fn,n_obs=dataset.n_obs, latent_size = latent_size, n_samples=opt.n_samples).to(device)
optimizer = optim.Adam(
            [
                {
                     "params":model.parameters(), "lr":opt.lr,
                },
                {
                     "params": latent_vecs, "lr":opt.lr,  
                },
                {
                     "params": w, "lr":opt.lr,
                }
            ]
            )
if opt.mode == 'double':
     optimizer = optim.Adam(
            [
                {
                     "params":model.parameters(), "lr":opt.lr,
                },
                {
                     "params": latent_vecs, "lr":opt.lr,
                },
                {
                     "params": w, "lr":opt.lr,
                },
                {
                     "params": w_r, "lr":opt.lr,
                }
            ]
            ) 
if opt.model is not None:
    utils.load_checkpoint(opt.model,model,optimizer)
def merge_op(fwd,back,op):
    if opt.op == 'cat':
        return torch.cat([fwd,back],0)
    elif opt.op == 'add':
        return fwd+back
    elif opt.op == 'maxpool':
        l = torch.cat([fwd,back],1)
        return torch.max(l,1) 

print('start training')
for epoch in range(opt.n_epochs):

    training_loss= 0.0
    model.train()
    for index,(obs_batch,valid_pt,index_latents) in enumerate(loader):
        obs_batch = obs_batch.to(device)
        valid_pt = valid_pt.to(device)
        latent_inputs = torch.zeros(0).to(device)
        indices = index_latents.cpu().detach().numpy()
        for i_lat in indices:
            latent = latent_vecs[i_lat]
            if i_lat == 0:
               latent = latent_vecs[0]
            elif i_lat > 0:
                latent = latent_vecs[i_lat] + w * latent_vecs[i_lat-1]
            latent_inputs = torch.cat([latent_inputs, latent.unsqueeze(1)], 1)
        #print(latent_inputs.size())
        if opt.mode == 'double':
            for i_lat in indices[::-1]:
                if i_lat == opt.batch_size-1:
                    continue
                elif i_lat < opt.batch_size-1:
                    latent_rev = w_r * latent_inputs[:,i_lat+1].clone()
                    latent_inputs[:,i_lat] = merge_op(latent_inputs[:,i_lat].clone(),latent_rev,opt.op)
 
        #latent_inputs=latent_inputs.permute(0,2,1)
        latent_inputs = latent_inputs.transpose(0,1) 
        obs_batch = obs_batch.to(device)
        loss = model(obs_batch,valid_pt,latent_inputs)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        training_loss += loss.item()
    
    training_loss_epoch = training_loss/len(loader)
    
    if (epoch+1) % opt.log_interval == 0:
        print('[{}/{}], training loss: {:.4f}'.format(
            epoch+1,opt.n_epochs,training_loss_epoch))

        obs_global_est_np = []
        pose_est_np = []
        with torch.no_grad():
            model.eval()
            for index,(obs_batch,valid_pt,index_latents) in enumerate(loader):
                latent_inputs = torch.zeros(0).cuda()
                for i_lat in index_latents.cpu().detach().numpy():
                    if i_lat == 0:
                        latent = latent_vecs[i_lat]	
                    elif i_lat > 0:
                        latent = latent_vecs[i_lat] +  w * latent_vecs[i_lat-1]
                    latent_inputs = torch.cat([latent_inputs, latent.unsqueeze(1)], 1)
                if opt.mode == 'double':
                    for i_lat in indices[::-1]:
                         if i_lat == instances_per_scene-1:
                             continue
                         elif i_lat < opt.batch_size-1:
                             latent_rev = w_r * latent_inputs[:,i_lat+1].clone()
                             latent_inputs[:,i_lat] = merge_op(latent_inputs[:,i_lat].clone(),latent_rev,opt.op)
                latent_inputs = latent_inputs.transpose(0,1)
                #print("sdsd",latent_inputs)
                obs_batch = obs_batch.to(device)
                valid_pt = valid_pt.to(device)
                model(obs_batch,valid_pt,latent_inputs)
                obs_global_est_np.append(model.obs_global_est.cpu().detach().numpy())
                pose_est_np.append(model.pose_est.cpu().detach().numpy())
            
            pose_est_np = np.concatenate(pose_est_np)
            if init_pose is not None:
                pose_est_np = utils.cat_pose_2D(init_pose_np,pose_est_np)

            save_name = os.path.join(checkpoint_dir,'model_best.pth')
            utils.save_checkpoint(save_name,model,optimizer)

            obs_global_est_np = np.concatenate(obs_global_est_np)
            kwargs = {'e':epoch+1}
            valid_pt_np = dataset.valid_points.cpu().detach().numpy()
            #utils.plot_global_point_cloud(obs_global_est_np,pose_est_np,valid_pt_np,checkpoint_dir,**kwargs)
            
            #save_name = os.path.join(checkpoint_dir,'obs_global_est.npy')
            #np.save(save_name,obs_global_est_np)

            save_name = os.path.join(checkpoint_dir,'pose_est.npy')
            np.save(save_name,pose_est_np)
            
    if (epoch+1) % (opt.log_interval*4) == 0:
        os.system('./run_eval_2D.sh')
