import torch
from torch import nn
import torchvision
from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning import Trainer
from dataset import FkDataset, Simulation
import torchvision.transforms as t
from torch.utils.data import DataLoader
from torchvision.utils import make_grid as mg
import utils
from utils import log, time_grad, space_grad_mse_loss, time_grad_mse_loss, energy_mse_loss, Elu, Downsample, Normalise, Rotate, Flip
import random
import math


class SplineActivation(nn.Module):
    def __init__(self, degree):
        super(SplineActivation, self).__init__()
        self.weights = nn.Parameter(torch.empty(degree + 1))
    
    def forward(self, x):
        for i in range(len(self.weights)):
            x = x + (x ** i) * self.weights[i]
        return x


class SoftAttention3D(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.project = nn.Conv3d(in_channels, in_channels, kernel_size=(3, 1, 1), padding=(1, 0, 0))

    def forward(self, x):
        return torch.sigmoid(self.project(x))


class SelfAttention3D(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.query = nn.Conv3d(in_channels, in_channels, kernel_size=(3, 1, 1), padding=(1, 0, 0))
        self.key = nn.Conv3d(in_channels, in_channels, kernel_size=(3, 1, 1), padding=(1, 0, 0))
        self.value = nn.Conv3d(in_channels, in_channels, kernel_size=(3, 1, 1), padding=(1, 0, 0))
        
    def forward(self, x):
        query = self.query(x)
        key = self.key(x)
        value = self.value(x)
        attention = torch.matmul(torch.transpose(query, -2, -1), key)
        attention = torch.softmax(attention, dim=1)
        attention = torch.matmul(attention, value)
        return attention

    
class ResidualBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, padding, attention="none", activation=0):
        super().__init__()
        self.conv = nn.Conv3d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, padding=padding)
        if activation:
            self.activation = SplineActivation(activation)
        else:
            self.activation = torch.nn.functional.elu
        if attention is None or attention.lower() == "none":
            self.attention = nn.Identity()
        elif "self" in attention:
            self.attention = SelfAttention3D(out_channels)
        else:
            self.attention = SoftAttention3D(out_channels)
        
    def forward(self, x):
        log("x: ", x.shape)
        a = self.attention(x)
        log("attention: ", a.shape)
        dx = self.conv(x)
        dx = self.activation(dx)
        log("conv: ", dx.shape)
        return dx + x
    
    
class ResNet(LightningModule):
    def __init__(self, n_layers, n_filters, kernel_size, residual_step, activation, frames_in, frames_out, step, loss_weights={}, attention="self", lr=0.001):
        super().__init__()
        self.save_hyperparameters()
        self.n_layers = n_layers
        self.n_filters = n_filters
        self.kernel_size = kernel_size
        self.residual_step  = residual_step
        self.activation = activation
        self.frames_in = frames_in
        self.frames_out = frames_out
        self.step = step
        self.loss_weights = loss_weights
        self.attention = attention
        self.lr = lr
        
        self._val_steps_done = 0
        
        padding = tuple([math.floor(x / 2) for x in kernel_size])
        self.inlet = nn.Conv3d(self.frames_in, n_filters, kernel_size=kernel_size, stride=1, padding=padding)
        
        self.flow = nn.ModuleList()
        for i in range(n_layers):
            self.flow.append(ResidualBlock(n_filters, n_filters, kernel_size=kernel_size, stride=1, padding=padding,
                                           attention=attention, activation=activation))
            
        self.outlet = nn.Conv3d(n_filters, 1, kernel_size=kernel_size, stride=1, padding=padding)
        
    def forward(self, x):
        x = self.inlet(x)
        queue = []
        for i, m in enumerate(self.flow):
            x = m(x)
            if not (i % self.residual_step):
                queue.append(x)
                if i != self.residual_step:
                    res = queue.pop()
                    x = x + res
        x = self.outlet(x)
        return x
    
    def backward(self, *args):
        # we're using truncated backprop through time, so we call the backward methods inside training_step
        # we override this to do nothing
        return
    
    def parameters_count(self):
        return sum(p.numel() for p in self.parameters())
    
    def get_loss(self, y_hat, y):
        recon_loss = torch.sqrt(nn.functional.mse_loss(y_hat, y, reduction="mean")) / y_hat.size(0) / y_hat.size(1)
        recon_loss = recon_loss * self.loss_weights.get("recon_loss", 1.)
        space_grad_loss = torch.sqrt(space_grad_mse_loss(y_hat, y, reduction="mean")) / y_hat.size(0) / y_hat.size(1)
        space_grad_loss = space_grad_loss * self.loss_weights.get("space_grad_loss", 1.)
        energy_loss = torch.sqrt(energy_mse_loss(y_hat, y, reduction="mean")) / y_hat.size(0) / y_hat.size(1)
        energy_loss = energy_loss * self.loss_weights.get("energy_loss", 1.)
        return {"recon_loss": recon_loss, "space_grad_loss": space_grad_loss, "energy_loss": energy_loss}
    
    def configure_optimizers(self):
        optimisers = [torch.optim.Adam(self.parameters(), lr=self.lr)]
        schedulers = [{
            'scheduler': torch.optim.lr_scheduler.ReduceLROnPlateau(optimisers[0], verbose=True, min_lr=1e-8),
            'monitor': 'loss'}]
        return optimisers, schedulers
    
    def training_step(self, batch, batch_idx):
        x = batch[:, :self.frames_in]
        y = batch[:, self.frames_in:]
        
        output_sequence = torch.empty_like(y)
        loss = {}
        for i in range(self.frames_out):
            # forward model
            output_sequence[:, i] = self(x).squeeze()
            
            # calculate loss
            current_loss = self.get_loss(output_sequence[:, i], y[:, i])
            total_loss = sum(current_loss.values())
            for k, v in current_loss.items():
                loss.update({k: (loss.get(k, 0.) + v).detach()})  # detach partial losses since they're not useful anymore for backprop
            
            # backward
            total_loss.backward(retain_graph=self.frames_out > 1)
            
            if (self.frames_out > 1):
                # update sequence
                output_sequence[:, i] = output_sequence[:, i].detach()
                x = torch.stack([x[:, -1], output_sequence[:, i]], dim=1).detach()
            
        # logging losses
        logs = {"train_loss/" + k: v for k, v in loss.items()}
        logs["train_loss/total_loss"] = total_loss
        return {"loss": total_loss, "val_loss": total_loss, "log": logs, "out": (batch[:, :self.frames_in], output_sequence, y)}
    
    def training_step_end(self, outputs):
        x, y_hat, y = outputs["out"]
        
        # log outputs as images
        i = random.randint(0, y_hat.size(0) - 1)
        nrow, normalise = 10, True
        self.logger.experiment.add_image("train_w/input", mg(x[i, :, 0].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_v/input", mg(x[i, :, 1].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_u/input", mg(x[i, :, 2].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_w/pred", mg(y_hat[i, :, 0].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_w/truth", mg(y[i, :, 0].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_v/pred", mg(y_hat[i, :, 1].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_v/truth", mg(y[i, :, 1].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_u/pred", mg(y_hat[i, :, 2].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("train_u/truth", mg(y[i, :, 2].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        return outputs    
    
    def train_epoch_end(self, outputs):
        # log model weights
        for i, module in enumerate(self.flow):
            # conv kernels
            self.logger.experiment.add_histogram("_conv", module.conv.weight, i)
            for c in range(self.kernel_size[0]):
                self.logger.experiment.add_image("kernel/layer-{}".format(i), mg(module.conv.weight[0, :, c].unsqueeze(1), nrow=self.n_filters, normalize=True), self.current_epoch)
            
            # log attention 
            if hasattr(module.attention, "project"):
                self.logger.experiment.add_histogram("_soft-attention", module.attention.project.weight, i)
                for c in range(self.kernel_size[0]):
                    self.logger.experiment.add_image("_soft-attention/layer-{}".format(i), mg(module.conv.weight[0, :, c].unsqueeze(1), nrow=self.n_filters, normalize=True), self.current_epoch)
            elif hasattr(module.attention, "query"):
                self.logger.experiment.add_histogram("_seft-attention/query", module.attention.query.weight, i)
                self.logger.experiment.add_histogram("_seft-attention/key", module.attention.key.weight, i)
                self.logger.experiment.add_histogram("_seft-attention/value", module.attention.value.weight, i)
                for c in range(self.kernel_size[0]):
                    self.logger.experiment.add_image("_soft-attention/query-{}".format(i), mg(module.attention.query.weight[0, :, c].unsqueeze(1), nrow=self.n_filters, normalize=True), self.current_epoch)
                    self.logger.experiment.add_image("_soft-attention/key-{}".format(i), mg(module.attention.key.weight[0, :, c].unsqueeze(1), nrow=self.n_filters, normalize=True), self.current_epoch)
                    self.logger.experiment.add_image("_soft-attention/value-{}".format(i), mg(module.attention.query.weight[0, :, c].unsqueeze(1), nrow=self.n_filters, normalize=True), self.current_epoch)     
        return outputs
    
    def validation_step(self, batch, batch_idx):
        batch = batch.float()
        x = batch[:, :self.frames_in]
        y = batch[:, self.frames_in:]
        
        output_sequence = torch.empty_like(y)
        loss = {}
        for i in range(self.frames_out):
            # forward model
            output_sequence[:, i] = self(x).squeeze()
            
            # calculate loss
            current_loss = self.get_loss(output_sequence[:, i], y[:, i])
            total_loss = sum(current_loss.values())
            for k, v in current_loss.items():
                loss.update({k: (loss.get(k, 0.) + v)})  # detach partial losses since they're not useful anymore for backprop
                        
            if (self.frames_out > 1):
                # update sequence
                x = torch.stack([x[:, -1], output_sequence[:, i]], dim=1)
            
        # logging losses
        logs = {"val_loss/" + k: v for k, v in loss.items()}
        logs["val_loss/total_loss"] = total_loss
        self._val_steps_done += 1
        return {"loss": total_loss, "log": logs, "out": (batch[:, :self.frames_in], output_sequence, y)}
    
    @torch.no_grad()
    def validation_step_end(self, outputs):
        x, y_hat, y = outputs["out"]
        
        # log loss
        for k, v in outputs["log"].items():
            self.logger.experiment.add_scalar(k, v, self._val_steps_done)
        
        # log outputs as images
        i = random.randint(0, y_hat.size(0) - 1)
        nrow, normalise = 10, True
        self.logger.experiment.add_image("val_w/input", mg(x[i, :, 0].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_v/input", mg(x[i, :, 1].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_u/input", mg(x[i, :, 2].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_w/pred", mg(y_hat[i, :, 0].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_w/truth", mg(y[i, :, 0].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_v/pred", mg(y_hat[i, :, 1].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_v/truth", mg(y[i, :, 1].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_u/pred", mg(y_hat[i, :, 2].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        self.logger.experiment.add_image("val_u/truth", mg(y[i, :, 2].unsqueeze(1), nrow=nrow, normalize=normalise), self.current_epoch)
        return outputs
        
def collate(batch):
    x = torch.stack([torch.as_tensor(t) for t in batch], 0)
    
#     # random rotate
    if random.random() > 0.5:
        x = torch.rot90(x, k=random.randint(1, 3), dims=(-2, -1))

#     # random flip
    if random.random() > 0.5:
        x = torch.flip(x, dims=(random.randint(-2, -1), ))
    return x


if __name__ == "__main__":
    from argparse import ArgumentParser
    
    parser = ArgumentParser()
    parser.add_argument('--frames_in', type=int, default=2)
    parser.add_argument('--frames_out', type=int, default=10)
    parser.add_argument('--step', type=int, default=5)
    parser.add_argument('--n_layers', type=int, default=10)
    parser.add_argument('--n_filters', type=int, default=4)
    parser.add_argument('--kernel_size', type=int, nargs='+', default=(1, 7, 7))
    parser.add_argument('--residual_step', type=int, default=5)
    parser.add_argument('--activation', type=int, default=0)
    parser.add_argument('--attention', type=str, default="none")
    parser.add_argument('--input_size', type=int, default=256)
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--precision', type=int, default=32)
    
    parser.add_argument('--debug', default=False, action="store_true")
    parser.add_argument('--root', type=str, default="/media/ep119/DATADRIVE3/epignatelli/deepexcite/train_dev_set/")
    parser.add_argument('--filename', type=str, default="/media/SSD1/epignatelli/train_dev_set/spiral_params5.hdf5")
    parser.add_argument('--gpus', type=str, default="0")
    parser.add_argument('--row_log_interval', type=int, default=10)
    parser.add_argument('--n_workers', type=int, default=3)
    parser.add_argument('--resume_from_checkpoint', type=str, default=None)
    
    parser.add_argument('--recon_loss', type=float, default=1.)
    parser.add_argument('--space_grad_loss', type=float, default=1.)
    parser.add_argument('--time_grad_loss', type=float, default=1.)
    parser.add_argument('--energy_loss', type=float, default=1.)
    
    args = parser.parse_args()    
    utils.DEBUG = args.debug
    
    loss_weights = {
        "recon_loss": args.recon_loss,
        "space_grad_loss": args.space_grad_loss,
        "energy_loss": args.energy_loss,
        "time_grad_loss": args.time_grad_loss
    }

    model = ResNet(n_layers=args.n_layers,
                   n_filters=args.n_filters,
                   kernel_size=tuple(args.kernel_size),
                   residual_step=args.residual_step,
                   activation=args.activation,
                   frames_in=args.frames_in, frames_out=args.frames_out, step=args.step,
                   loss_weights=loss_weights,
                   attention=args.attention,
                   lr=args.lr)
    
    log(model)
    log("parameters: {}".format(model.parameters_count()))
    
    train_fkset = FkDataset(args.root, args.frames_in, args.frames_out, args.step, transform=Normalise(), squeeze=True, keys=["spiral_params3.hdf5", "three_points_params3.hdf5"])
    val_fkset = FkDataset(args.root, args.frames_in, args.frames_out, args.step, transform=Normalise(), squeeze=True, keys=["heartbeat_params3.hdf5"])
    
    train_loader = DataLoader(train_fkset, batch_size=args.batch_size, collate_fn=collate, shuffle=True, num_workers=args.n_workers)
    val_loader = DataLoader(val_fkset, batch_size=args.batch_size, collate_fn=collate, num_workers=args.n_workers)

    trainer = Trainer.from_argparse_args(parser,
                                         fast_dev_run=args.debug,
                                         default_root_dir="lightning_logs/resnet",
                                         profiler=args.debug)
    trainer.fit(model, train_dataloader=train_loader, val_dataloaders=val_loader)
    