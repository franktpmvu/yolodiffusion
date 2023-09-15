import os
import cv2
import numpy as np
import torch
import torch.utils.data
import torch.nn as nn
import yaml
import math
from tqdm import tqdm

from utils.general import non_max_suppression
from augmentations import mix_augmentaion
from matplotlib import pyplot as plt
import sys
sys.path.append('/data/licence_plate/Cold-Diffusion-Models/licenceplate_deaug_yolov7_pytorch/')

from licenceplate_deaug_pytorch.licenceplate_deaug_pytorch_aug_in_dataloader import Unet, GaussianDiffusion, Trainer

weight = 'runs/train/exp2/weights/best_288.pt'
data = 'data/plate.yaml'
input_size = 1024
conf_thres = 0.5
iou_thres = 0.5
device = 'cuda:2'
aug_licence = mix_augmentaion()

with open(data) as f:
    data = yaml.load(f, Loader=yaml.FullLoader)
classes = data['names']


def resize(img, size):
    h, w, c = img.shape
    if not (h == size and w == size):
        img = img.copy()
        scale_x = float(size / w)
        scale_y = float(size / h)
        ratio = min(scale_x, scale_y)
        nw, nh = int(w*ratio), int(h*ratio)
        new_img = cv2.resize(img, (nw, nh))

        blank = np.zeros((size, size, c))
        dw, dh = (size-nw)//2, (size-nh)//2
        blank[dh: dh+nh, dw: dw+nw] = new_img
        meta = {'nw': nw, 'nh': nh, 'dw': dw, 'dh': dh, 'w': w, 'h': h}
        return blank, meta
    else:
        meta = {}
        return img, meta

def warp_affine(pt, M):
    new_pt = np.array([pt[0], pt[1], 1.], dtype=np.float32).T
    new_pt = np.dot(M, new_pt)
    return new_pt

def affine_transform(dets, meta):
    '''
    Transfer input-sized perdictions to original-sized coordinate. (Anchor)
    Input:
        dets = [1(batch), num_objs, 6(x1, y1, x2, y2, conf, cls)]
        meta = {'nw': resize_w, 
                'nh': resize_h, 
                'dw': offset_w, 
                'dh': offset_h, 
                'w': original_size_w, 
                'h': original_size_h}
    '''
    dets = np.array([x.cpu().numpy() for x in dets[0]])
    if len(meta)>0:
        p1 = np.float32([[0, 0], [0, meta['nh']], [meta['nw'], 0]])
        p2 = np.float32([[0, 0], [0, meta['h']], [meta['w'], 0]])
        M = cv2.getAffineTransform(p1, p2)

        for i in range(dets.shape[0]):
            dets[i, 0] -= meta['dw']
            dets[i, 1] -= meta['dh']
            dets[i, 2] -= meta['dw']
            dets[i, 3] -= meta['dh']

            dets[i, 0:2] = warp_affine(dets[i, 0:2], M)
            dets[i, 2:4] = warp_affine(dets[i, 2:4], M)
        return dets
    else:
        return dets

def preproccess_img(img_path):
    img = cv2.imread(img_path)
    img, meta = resize(img, input_size)
    img = (img / 255.).astype(np.float32)
    img = img[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB, to 3x512x512
    img = np.ascontiguousarray(img)
    img = torch.from_numpy(img).to(device)
    img = torch.unsqueeze(img, 0)
    #img = img.half()
    return img, meta

def run(img_path):
    '''
    Return: output = [num_objs, 6(x1, y1, x2, y2, conf, cls)]
    '''
    img, meta = preproccess_img(img_path)
    with torch.no_grad():
        ret = model(img)
        ret = non_max_suppression(ret[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)
    return ret, meta

def run_test_bfaf(img_path):
    '''
    Return: output = [num_objs, 6(x1, y1, x2, y2, conf, cls)]
    '''
    img, meta = preproccess_img(img_path)
    with torch.no_grad():
        ret1 = model(img)
        #ret1 = non_max_suppression(ret1[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)

        ret2_bf,y = model.forward_submodel(img,model.before_diffusion_model,output_y=True)
        print(ret2_bf)
        ret2 = model.forward_submodel(ret2_bf,model.after_diffusion_model,init_y=y)
        #ret2 = non_max_suppression(ret2[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)
    return ret1, ret2, meta

def change_yolo_detect_to_1d(_layer,batch_size):
    _layer0 = _layer[0].view(batch_size,-1)
    _layer1 = _layer[1][0].view(batch_size,-1)
    _layer2 = _layer[1][1].view(batch_size,-1)
    _layer3 = _layer[1][2].view(batch_size,-1)
    z = [_layer0,_layer1,_layer2,_layer3]
    
    output_layer = torch.cat(z,1)
    return output_layer

def run_test_pair_loss(img_path,times=99,ratio=1.0):
    '''
    Return: output = [num_objs, 6(x1, y1, x2, y2, conf, cls)]
    '''
    img, meta = preproccess_img(img_path)
    img_aug = aug_licence.batch_data_add_licence_aug(img, ratio)
    #loss = nn.CrossEntropyLoss()

    with torch.no_grad():
        ret1 = model(img)
        #ret1 = non_max_suppression(ret1[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)

        ret2_bf,y2 = model.forward_submodel(img,model.before_diffusion_model,output_y=True)
        batch_size = ret2_bf.shape[0]
        y2[-1]=ret2_bf
        ret2 = model.forward_submodel(ret2_bf,model.after_diffusion_model,init_y=y2)

        ret3_bf,y3 = model.forward_submodel(img_aug,model.before_diffusion_model,output_y=True)
        batch_size = ret3_bf.shape[0]
        step = torch.full((batch_size,), times - 1, dtype=torch.long).cuda().to(device)#.half()
        ret_diffusion3 = diffusion_trainer.model.module.denoise_fn(ret3_bf,step)
        y3[-1]=ret_diffusion3
        ret3 = model.forward_submodel(ret_diffusion3,model.after_diffusion_model,init_y=y3)
        
            
        ret1_1d = change_yolo_detect_to_1d(ret1,batch_size)
        ret2_1d = change_yolo_detect_to_1d(ret2,batch_size)
        ret3_1d = change_yolo_detect_to_1d(ret3,batch_size)
        
        output_loss_sameimg = (ret1_1d - ret2_1d).abs().mean()
        
        output_loss_diffimg = (ret1_1d - ret3_1d).abs().mean()

        print(output_loss_sameimg)
        print(output_loss_diffimg)
        #ret2 = non_max_suppression(ret2[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)

def run_withdiffusion(img_path,times=99):
    '''
    Return: output = [num_objs, 6(x1, y1, x2, y2, conf, cls)]
    '''
    img, meta = preproccess_img(img_path)
    with torch.no_grad():
        ret_bf,y = model.forward_submodel(img,model.before_diffusion_model,output_y=True)
        batch_size = ret_bf.shape[0]
        step = torch.full((batch_size,), times - 1, dtype=torch.long).cuda().to(device)#.half()
        
        ret_diffusion = diffusion_trainer.model.module.denoise_fn(ret_bf,step)
        
        #print('ret_bf = '+str(torch.mean(ret_bf))+str(torch.min(ret_bf))+str(torch.max(ret_bf)))
        #print('ret_diffusion = '+str(torch.mean(ret_diffusion))+str(torch.min(ret_diffusion))+str(torch.max(ret_diffusion)))

        #ret_diffusion = ret_bf
        y[-1]=ret_diffusion
        ret = model.forward_submodel(ret_diffusion,model.after_diffusion_model,init_y=y)
        ret = non_max_suppression(ret[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)
    return ret, meta

def run_withdiffusion_ema(img_path,times=100):
    '''
    Return: output = [num_objs, 6(x1, y1, x2, y2, conf, cls)]
    '''
    img, meta = preproccess_img(img_path)
    with torch.no_grad():
        ret_bf,y = model.forward_submodel(img,model.before_diffusion_model,output_y=True)
        batch_size = ret_bf.shape[0]
        step = torch.full((batch_size,), times - 1, dtype=torch.long).cuda().to(device)#.half()
        
        ret_diffusion = diffusion_trainer.ema_model.module.denoise_fn(ret_bf,step)
        
        #print('ret_bf = '+str(torch.mean(ret_bf))+str(torch.min(ret_bf))+str(torch.max(ret_bf)))
        #print('ret_diffusion = '+str(torch.mean(ret_diffusion))+str(torch.min(ret_diffusion))+str(torch.max(ret_diffusion)))

        #ret_diffusion = ret_bf
        y[-1]=ret_diffusion
        ret = model.forward_submodel(ret_diffusion,model.after_diffusion_model,init_y=y)
        ret = non_max_suppression(ret[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)
    return ret, meta


def run_aug_withdiffusion(img_path,times=100,ratio=1.0):
    '''
    Return: output = [num_objs, 6(x1, y1, x2, y2, conf, cls)]
    '''
    img, meta = preproccess_img(img_path)
    img = aug_licence.batch_data_add_licence_aug(img, ratio)

    with torch.no_grad():
        ret_bf,y = model.forward_submodel(img,model.before_diffusion_model,output_y=True)
        batch_size = ret_bf.shape[0]
        step = torch.full((batch_size,), times - 1, dtype=torch.long).cuda().to(device)#.half()
        
        #ret_diffusion = diffusion.denoise_fn(ret_bf, step)
        ret_diffusion = diffusion_trainer.model.module.denoise_fn(ret_bf,step)
        
        y[-1]=ret_diffusion
        ret = model.forward_submodel(ret_diffusion,model.after_diffusion_model,init_y=y)
        ret = non_max_suppression(ret[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)
    return ret, meta



def run_aug(img_path,ratio=1.0):
    '''
    Return: output = [num_objs, 6(x1, y1, x2, y2, conf, cls)]
    '''
    img, meta = preproccess_img(img_path)
    b, c, h, w = img.shape
    aug_licence.imshape=(h,w)
    aug_licence.random_parameter()

    img = aug_licence.batch_data_add_licence_aug(img, ratio)
    with torch.no_grad():
        ret = model(img)
        ret = non_max_suppression(ret[0], conf_thres=conf_thres, iou_thres=iou_thres, labels=[], multi_label=True)
    return ret, meta

#==================================================================

def transfer_label(lab):
    '''
    Transfer label(string) into list(int).
    Input:
        lab: string list ['x_min,y_min,x_max,y_max,plate']
            i.e. ['14,71,83,105,FS799', '215,188,324,240,DP4846']
    Return:
        new_lab: int list [x_min, y_min, x_max, y_max]
            i.e. [[14,71,83,105], [215,188,324,240]]
    '''
    new_lab = []
    for l in lab:
        _l = l.split(',')
        x1 = int(_l[0])
        y1 = int(_l[1])
        x2 = int(_l[2])
        y2 = int(_l[3])
        new_lab.append([x1, y1, x2, y2])
    
    return new_lab

def get_iou(bb1, bb2):
    '''
    Input:
        bb1(groundtruth) = [left_top_x, y, right_bottom_x, y]
        bb2(predict_point) = [left_top_x, y, right_bottom_x, y]
    '''
    x_left = max(bb1[0], bb2[0])
    y_top = max(bb1[1], bb2[1])
    x_right = min(bb1[2], bb2[2])
    y_bottom = min(bb1[3], bb2[3])

    if x_right < x_left or y_bottom < y_top:
        return 0.0

    intersection_area = (x_right - x_left) * (y_bottom - y_top)

    bb1_area = (bb1[2] - bb1[0]) * (bb1[3] - bb1[1])
    bb2_area = (bb2[2] - bb2[0]) * (bb2[3] - bb2[1])

    iou = intersection_area / float(bb1_area + bb2_area - intersection_area)
    assert iou >= 0.0
    assert iou <= 1.0
    return iou

def check_bbox(bb1, bb2):
    '''
    Check if bb2 is completely covered by bb1.
    If so, return True, otherwise return False.
    Input:
        bb1(large region) = [x_min, y_min, x_max, y_max] (all int)
        bb2(small region) = [x_min, y_min, x_max, y_max] (all int)
    '''
    # Assert bb1 is the larger one
    bb1_area = (bb1[2] - bb1[0]) * (bb1[3] - bb1[1])
    bb2_area = (bb2[2] - bb2[0]) * (bb2[3] - bb2[1])
    if bb1_area < bb2_area:
        temp = bb2
        bb2 = bb1
        bb1 = temp
    
    f1 = bb1[0] <= bb2[0]
    f2 = bb1[1] <= bb2[1]
    f3 = bb1[2] >= bb2[2]
    f4 = bb1[3] >= bb2[3]

    if f1 and f2 and f3 and f4:
        return True
    else:
        return False

def group_plate(outp):
    '''
    Group each character into seperate plates.
    Input:
        outp = float[[x1, y1, x2, y2, score, class], ...]
            i.e. [[     40.859      162.66      142.42      216.09      0.9668          34]
                  [     125.08      178.28      138.83      208.91     0.93213           6]
                  [         95      176.41      110.94      207.03      0.9248           8]
                  [     110.16      177.34      125.16      208.28     0.91211           8]
                  [     80.156      175.47      97.344      206.72     0.90674           4]
                  [     45.039      173.44      64.414         205     0.89111          13]
                  [     60.078      174.06      78.828      205.62     0.81445          17]]
    Return: 
        a sorted list of dict [{'plate': int[x1, y1, x2, y2], 'char':[int[x1, y1, x2, y2, idx], ...]}, ...]
             i.e. [{'plate': [40, 162, 142, 216], 
                    'char':  [[45, 173, 64, 205, 13],
                             [60, 174, 78, 205, 17],
                             [80, 175, 97, 206, 4],
                             [95, 176, 110, 207, 8],
                             [110, 177, 125, 208, 8],
                             [125, 178, 138, 208, 6]]}]
    '''
    plates = []
    chars = []
    groups = []
    for obj in outp:
        if int(obj[-1]) == 34:
            pla = [int(p) for p in obj[:4]]
            group = {'plate': pla, 'char':[]}
            groups.append(group)
        else:
            chars.append(obj)
    for obj in chars:
        cha = [int(obj[0]), int(obj[1]), int(obj[2]), int(obj[3]), int(obj[5])]
        for g in groups:
            pla = g['plate']
            if check_bbox(pla, cha[:4]):
                g['char'].append(cha)
    ### Sort list
    for g in groups:
        g['char'] = sorted(g['char'], key=lambda x: x[0])
    return groups

def get_str(chars):
    '''
    Create plate string from indivisual detected character.
    Input:
        chars = a sorted list [int[x1, y1, x2, y2, idx], ...]]
    Return:
        plate_str = plate string, i.e. 'DH4886'
    '''
    s = ''
    for o in chars:
        c = o[-1]
        s += classes[c]
    return s

def get_acc(inp, outp):
    """
    Compute two strings character by character.
    Input:
        inp: ground truth(str)
        outp: detected result(str)
    Return:
        m: number of groundtruth(int)
        count: number of detect correctly(int)
    """
    m = len(inp)
    count = sum((Counter(inp) & Counter(outp)).values())
    return m, count

def compute_acc(outp, labels):
    '''
    Compute accuracy between detected results and labels.
    Input:
        outp = a list of dic [{'plate': int[x1, y1, x2, y2], 'char':[int[x1, y1, x2, y2, idx], ...]}, ...]
        labels = a list of str, i.e. ['14,71,83,105,FS799', '215,188,324,240,DP4846']
    Return:
        total = the number of all characters in labels
        correct = the number of correct-detected characters
    '''
    total = 0
    correct = 0
    for label in labels:
        line = label.split(',')
        plate_gt = [int(x) for x in line[:4]]
        for g in outp:
            if get_iou(plate_gt, g['plate']) >= 0.5:
                detected_plate = get_str(g['char'])
                t, c = get_acc(line[-1], detected_plate)
                total += t
                correct += c
    return total, correct

def labels_len(labels):
    '''
    Compute the number of characters in ground truth.
    Input:
        labels = a list of str, i.e. ['14,71,83,105,FS799', '215,188,324,240,DP4846']
    Return:
        num = the number of characters of all plates
    '''
    num = 0
    for label in labels:
        lines = label.split(',')
        n = len(lines[-1])
        num += n
    return num

def get_wer(r, h):
    """
    Compute word_error_rate(WER) of two list of strings.
    Input:
        r = ground truth
        h = predicted results
    Return:
        result = WER (presented in percentage)
        sid = substitution + insertion + deletion
        total = the number of groundtruth
    """
    d = np.zeros((len(r) + 1) * (len(h) + 1), dtype=np.uint16)
    d = d.reshape((len(r) + 1, len(h) + 1))
    for i in range(len(r) + 1):
        for j in range(len(h) + 1):
            if i == 0:
                d[0][j] = j
            elif j == 0:
                d[i][0] = i

    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            if r[i - 1] == h[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                substitution = d[i - 1][j - 1] + 1
                insertion = d[i][j - 1] + 1
                deletion = d[i - 1][j] + 1
                d[i][j] = min(substitution, insertion, deletion)
    sid = d[len(r)][len(h)]
    total = len(r)
    result = float(sid) / total * 100

    return result, sid, total

def eval_dataset(results,label_data):
    correct_plate = {}
    total_p, correct_p, pred_p = 0, 0, 0
    for k, v in label_data.items():
        gt = transfer_label(v)
        total_p += len(gt)
        if k in results:
            r = group_plate(results[k]) # r = [{'plate': int[x1, y1, x2, y2], 'char':[int[x1, y1, x2, y2, label], ...]}, ...]
            pred_p += len(r)
            correct_r = []
            for _r in r:
                for idx, bbox in enumerate(gt):
                    if get_iou(bbox, _r['plate']) >= 0.5:
                        n_r = {'plate': _r['plate'], 'char': _r['char'], 'idx': idx}
                        correct_r.append(n_r)
            num = len(correct_r)
            if num > 0:
                correct_plate[k] = correct_r
                correct_p += num
                
    print("Number of Correctly Detected Plates =", correct_p)
    print("Number of Detected Plates =", pred_p)
    print("Number of All Plates =", total_p)
    print("Recall = {:.4f}".format(correct_p/total_p))
    print("Precision = {:.4f}".format(correct_p/pred_p))

    n_perfect = 0  ### number of perfectly recognized plates
    n_sid = 0  ### number of failed recognized chars in detected
    n_detected = 0  ### number of chars in detected plates

    for k, v in label_data.items():
        gt_strs = [s.split(',')[-1] for s in v]
        if k in correct_plate:
            objs = correct_plate[k]
            for obj in objs: # obj = [{'plate': int[x1, y1, x2, y2], 'char':[int[x1, y1, x2, y2, label], ...], 'idx': int(i)}]
                pred_str = [classes[x[-1]] for x in obj['char']]
                wer, sid, t = get_wer(list(gt_strs[obj['idx']]), pred_str)
                n_sid += sid
                n_detected += t
                if wer == 0:
                    n_perfect += 1


    print("Characters in Detected Plates = ", n_detected)
    print("Error Characters (Detected) =", n_sid)
    print("World Error Rate (Detected) = {:.4f}".format(n_sid/n_detected))

    print("\nNumber of Perfectly Recognized Plates = ", n_perfect)
    print("Accuracy(Detected) = {:.4f}".format(n_perfect/correct_p))
    print("Accuracy(Groundtruth) = {:.4f}".format(n_perfect/total_p))
    
"""
Create label dictionary.
Format: dic = {key: file_name(str), value: [obj1(str), obj2(str), ...]}
        obj format = 'x_min, y_min, x_max, y_max, plate'
   i.e. dic['train_LE_3'] = ['266,199,350,242,2972KK']
        dic['train_LE_33'] = ['14,71,83,105,FS799', '215,188,324,240,DP4846']
"""
label_data = {}
#label_txt = '/data/licence_plate/_plate/AOLP/label.txt'
label_txt = '/data/licence_plate/_plate/generated_data/result2/label.txt'

#label_txt = 'E:/MTL_FTP/ChengJungC/dataset/AOLP/label.txt'
#label_txt = 'E:/MTL_FTP/ChengJungC/dataset/weather/label.txt'
label_file = open(label_txt, 'r')
lines = label_file.readlines()
for line in lines:
    l = line.strip().split(' ')
    name = l[0]
    plates = l[1:]
    label_data[name] = plates
    
#img_dir = '/data/licence_plate/_plate/AOLP/original/'
img_dir = '/data/licence_plate/_plate/generated_data/result2/img/'

img_paths = os.listdir(img_dir)
img_paths.sort()
#model.create_subnetwork()
#model.before_diffusion_model.half()
#model.after_diffusion_model.half()
import copy
#import imageio
from PIL import Image


def resize_batch(img, size):
    #print(img.shape)
    #img_npy = x.cpu().numpy()
    new_img = [torch.from_numpy(np.transpose(cv2.resize(np.transpose(x.cpu().numpy(),(1, 2, 0)), (size, size)),(2,0,1))).to(device) for x in img]
    return img

def to_numpy(img):
    np_img=[np.transpose(x.cpu().numpy(),(1, 2, 0)) for x in img]
    return np_img

gif = []
for f in img_paths:
    if f.endswith('.jpg'):
        bname = os.path.splitext(f)[0]
        img_p = img_dir + f
        
        img, meta = preproccess_img(img_p)
        b, c, h, w = img.shape
        aug_licence.imshape=(h,w)
        aug_licence.random_parameter()
        
        for ratio in range(100):
            img_af = aug_licence.batch_data_add_licence_aug(copy.deepcopy(img), ratio*0.01)
            print("save in ./output/img_blur_%d.jpg"%(ratio))
            #print(to_numpy(img)[0].shape)
            plt.imsave("./output/img_blur_%d.jpg"%(ratio), to_numpy(img_af)[0])
            gif.append(Image.fromarray((to_numpy(img_af)[0]*255).astype(np.uint8)))
        break

gif[0].save("./output/array.gif", save_all=True, append_images=gif[1:], duration=50, loop=0)


