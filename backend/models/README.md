离线图片标签模型

- efficientdet_lite0.onnx: https://github.com/namas191297/efficientdetlite/tree/main/onnx_models
- efficientnet_lite0.onnx: https://github.com/andestech/ModelZoo/tree/master/EfficientNet-Lite0
- imagenet_classes.txt: https://github.com/pytorch/hub/blob/master/imagenet_classes.txt

运行时只读取这些本地文件，不调用云端识别服务。安装 backend/requirements.txt 后可在「设置 → 媒体与存档」开启。模型首次加载可能需要数秒；图片在独立后台线程逐步补标。
