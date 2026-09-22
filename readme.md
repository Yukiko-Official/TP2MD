# 基于Selenium的培养方案转Markdown文件

从西电一站式服务大厅抓取个人培养方案，清洗所得JSON得到干干净净的MD文件，方便你统计自己的学分情况。

感谢大肥鱼的帮助。

## 功能

手动登录ehall，我还没能做出自动人机验证的本事。

导出干净的JSON（方便电脑处理）和Markdown文件（方便能工智人处理）。

## 使用方法

看见右边那个Release没，去那下载，我已经打包好了可执行文件了。

最终结果应该如图所示（记得手动删除一下不符合你课程情况的英语课，以及手动添加自己的通识选修课部分）：

<img width="933" height="1032" alt="QQ_1789485622771" src="https://github.com/user-attachments/assets/081c23be-f702-41e1-903d-7dfb76ef4c7a" />

## 如果你要自己用的话

```
1. 克隆仓库
git clone https://github.com/Yukiko-Official/TP2MD.git
cd 仓库名

2. 安装依赖
pip install -r requirements.txt

3. 运行
python 培养方案.py
```

## 如果杀毒软件报毒

毕竟是一个臭大学生写的东西，当然得不到杀毒软件的认可了。

用别怕，怕别用，我对你的培养方案没有什么想法。
