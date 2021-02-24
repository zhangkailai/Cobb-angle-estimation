# --------------------------------------------------------
# Relation Networks for Object Detection
# Copyright (c) 2017 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiayuan Gu, Dazhi Cheng, Guodong Zhang
# --------------------------------------------------------
import os
import sys
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ['MXNET_CUDNN_AUTOTUNE_DEFAULT'] = '0'
os.environ['MXNET_ENABLE_GPU_P2P'] = '0'
#os.environ['MXNET_ENGINE_TYPE'] = 'NaiveEngine'
this_dir = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(this_dir, '..', '..', 'relation_rcnn'))

#print ('hello')
import train_end2end
#print ('hello')
import test

if __name__ == "__main__":
    train_end2end.main()
    test.main()




