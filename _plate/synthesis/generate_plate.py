import sys
import glob
import numpy as np
import os
sys.path.append('/data/licence_plate/_yolo/yolov7/')
from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle
from utils.generator import plate_generator
from utils.general import xyxy2xywh
from utils.augmentations import mix_augmentaion
import cv2
import concurrent.futures
import os
from PIL import Image
import random
import time

aug_licence = mix_augmentaion()
aug_licence.imshape=(512,512)
aug_licence.random_parameter()

#image_generator = plate_generator(augmentor=aug_licence)
image_generator = plate_generator()


def generate_plate():
    # Set a unique random seed for each process
    # Using a combination of the current time and the process ID ensures uniqueness
    random_seed = (int(time.time() * 1000) + os.getpid()) % (2**32)
    random.seed(random_seed)
    np.random.seed(random_seed)

    img, msg = image_generator.get_plate()
    return img, msg

def multi_cpu_for_loop(n=100, output_dir='result_350k'):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with concurrent.futures.ProcessPoolExecutor() as executor:
        futures = [executor.submit(generate_plate) for _ in range(n)]

        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            img, msg = future.result()

            # Format the filename as '%08d'
            base_filename = f'{i:08d}'
            img_path = os.path.join(output_dir, base_filename + '.jpg')
            txt_path = os.path.join(output_dir, base_filename + '.txt')

            # Save image using cv2.imwrite
            cv2.imwrite(img_path, img)

            # Save label to a text file
            with open(txt_path, 'w') as file:
                flattened_msg = [str(item) for sublist in msg for item in sublist]
                formatted_msg = ','.join(flattened_msg)
                file.write(formatted_msg + '\n')

    print(f'Images and labels saved in {output_dir}')

# Call the function
multi_cpu_for_loop(100, 'opt')