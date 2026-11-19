"""补录脚本模板：给已经跑完产出过、但当时还没有这套登记机制的老数据，
事后补一次register_artifact调用。不需要重新跑pipeline，只要把已有的
输出文件路径对应起来就行。

用法：把下面JOBS列表改成你自己要补录的那几组(视频路径, 产物路径)，
照抄一样数量的行不够用就加/不够用就删，然后：
    python backfill_register_artifacts.py

code_commit_override默认留None——老数据大概率想不起来当时是哪个commit，
显式留空比瞎填"现在的commit"更诚实(那是误导，不是不精确)。如果某一条你
恰好记得个大概(哪怕只是前几位)，把对应那一行的None换成那个字符串，
会自动用git校验补全成完整hash，解析不出来也不会阻止这条正常注册。
"""
from artifact_registration import register_artifact

JOBS = [
    {"video": "/mnt/.../video1.MP4", "output": "/mnt/.../video1_STD_TRK.csv", "code_commit_override": None},
    {"video": "/mnt/.../video2.MP4", "output": "/mnt/.../video2_STD_TRK.csv", "code_commit_override": None},
    {"video": "/mnt/.../video3.MP4", "output": "/mnt/.../video3_STD_TRK.csv", "code_commit_override": None},
    {"video": "/mnt/.../video4.MP4", "output": "/mnt/.../video4_STD_TRK.csv", "code_commit_override": None},
    {"video": "/mnt/.../video5.MP4", "output": "/mnt/.../video5_STD_TRK.csv", "code_commit_override": None},
]

if __name__ == "__main__":
    for job in JOBS:
        register_artifact(
            "ad_perception_tracking",
            [job["video"]],
            job["output"],
            {},
            code_commit_override=job["code_commit_override"],
        )
        print(f"补录完成: {job['video']} -> {job['output']}")
