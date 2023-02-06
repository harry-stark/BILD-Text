from cc2dataset import cc2dataset
import os

if __name__ == "__main__":
    # if you have a slurm cluster, refer to https://gist.github.com/rom1504/67ada3dedbecc113ae2dbdfd9c642d83 to start a spark cluster there
    cc2dataset(
        "s3a://s-laion/bild_text/try15",
        wat_index_count=1,
        master="spark://cpu128-dy-c6i-32xlarge-34:7077",
        num_cores=128,
        mem_gb=256,
        wat_count=500,
    )
