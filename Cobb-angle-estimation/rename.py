import os

path = './data/coco/images/val2017'

files = os.listdir(path)

for name in files:
    ori = os.path.join(path,name)
    new = os.path.join(path,'COCO_val2017_'+name)
    os.rename(ori, new)


