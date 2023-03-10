"""Easily convert common crawl to image caption set using pyspark"""


from fastwarc.warc import ArchiveIterator, WarcRecordType
import simdjson
import fsspec
from timeit import default_timer as timer
from loguru import logger
import hashlib
import datetime
from multiprocessing.pool import ThreadPool
from pyspark import SparkContext
from pyspark.sql.functions import rand
from pyspark.sql import SparkSession
import random
import math
import time
from .spark_session_builder import build_spark_session
from io import BytesIO


def extract_documents_from_wet(stream):
    """Extract document from stream"""
    all_extend = []
    try:
        for record in ArchiveIterator(stream):

            if record.headers['Content-Type'] == 'text/plain':
                wet_text = record.reader.read().decode('utf-8')
                all_extend.append({"text":wet_text,"url":record.headers['WARC-Target-URI'],"uid":str(hashlib.md5((wet_text).encode()).hexdigest())})


    except Exception as e:  # pylint: disable=broad-except
        logger.info(e)
        logger.info("A shard failed to parse")
        return []

    return all_extend


def process_wet(path):
    """Process a single wet file"""
    begin_read = timer()
    with fsspec.open(path, "rb") as f:
        for i in range(10):
            try:
                tf = BytesIO(f.read())
                break
            except Exception as ex:  # pylint: disable=broad-except
                if i == 9:
                    logger.info("failed 10 times, skipping ", path)
                    return
                logger.info(ex)
                logger.info(f"retrying reading {i}/10")
                time.sleep(1)

        for e in extract_documents_from_wet(tf):
            yield (e["uid"], e["url"], e["text"])
    end_read = timer()
    tot_read_time = end_read - begin_read
    logger.info(f"Took {tot_read_time} to parse")


def get_cc_wat_links(source_cc_protocol):
    """Get cc wat links"""
    if source_cc_protocol == "s3":
        fs, p = fsspec.core.url_to_fs("s3://commoncrawl/crawl-data/")
        links = ["s3://" + e for e in fs.glob(p + "/*/wet.paths.gz")]
        return links
    elif source_cc_protocol == "http":
        fs, p = fsspec.core.url_to_fs("https://commoncrawl.org/the-data/get-started/")
        a = fs.open(p).read()
        l = a.splitlines()
        l = [e.decode("utf8").replace("[WARC] ", "") for e in l]
        l = [e for e in l if "<li>s3://commoncrawl/crawl-data/" in e]
        l = [
            e.split(" ")[0].replace("<li>s3://commoncrawl/", "https://data.commoncrawl.org/").replace("<wbr>", "")
            for e in l
        ]
        l = [(e + "/wat.paths.gz").replace("//wat", "/wat") for e in l]
        return l
    else:
        raise ValueError(f"Unknown protocol {source_cc_protocol}")


def read_wat_index_file(wat_index):
    with fsspec.open(wat_index, "rb", compression="gzip") as f:
        wats = [a.decode("utf8").strip() for a in f.readlines()]
    return wats


def read_wat_index_files(shard_count, wat_count, source_cc_protocol):
    """Read all wat index files"""
    cc_wat_links = get_cc_wat_links(source_cc_protocol)
    if shard_count is not None:
        cc_wat_links = cc_wat_links[-shard_count:]  # pylint: disable=invalid-unary-operand-type
    all_wats = []
    with ThreadPool(16) as pool:
        for wats in pool.imap_unordered(read_wat_index_file, cc_wat_links):
            all_wats.extend(wats)
    if wat_count is not None:
        all_wats = random.choices(all_wats, k=wat_count)
    else:
        # shuffle to increase duplication over each part hence reduce size of each part after duplication
        random.shuffle(all_wats)
    return all_wats


def deduplicate_repartition_count(df, output_path, wat_count, spark, shuffle=False):
    """Deduplicate and repartition"""
    logger.info(f"Size Before : {df.count()}")
    uniques = df.dropDuplicates(["uid"])
    s = time.time()
    if shuffle:
        uniques = uniques.sort(rand())
    repartitioned = uniques.repartition(max(256, wat_count // 500))
    repartitioned.write.mode("overwrite").parquet(output_path)
    e = time.time()
    logger.info(f"Took {e - s} seconds")
    logger.info("Computing size")
    df = spark.read.parquet(output_path)
    logger.info(f"Size: {df.count()}")


def process_one_part(output_path, wat_index_files, build_spark, shuffle, document_type, source_cc_protocol):
    """Process one part"""
    spark = build_spark()
    sc = SparkContext.getOrCreate()
    wat_count = len(wat_index_files)
    wat_rdd = sc.parallelize(wat_index_files, wat_count)
    if source_cc_protocol == "s3":
        prefix = "s3://commoncrawl/"
    elif source_cc_protocol == "http":
        prefix = "https://data.commoncrawl.org/"

    def extract(x):
        x = list(x)
        yield from process_wet(prefix + x[0])

    output = wat_rdd.mapPartitions(extract)
    df = output.toDF(["uid", "url", "text"])

    deduplicate_repartition_count(df, output_path, wat_count, spark, shuffle)


def get_last_successful_part(output_path):
    """Get the last successful part"""
    output_path = output_path.replace("s3a", "s3")
    fs, _ = fsspec.core.url_to_fs(output_path)
    successful_parts = fs.glob(output_path + "/*/_SUCCESS")
    last_part = sorted([int(e.split("/")[-2].split("_")[-1]) for e in successful_parts if "merged" not in e])[-1]
    return last_part


def process_multi_part(
    output_path, wat_index_files, build_spark, multipart, shuffle, resume, document_type, source_cc_protocol
):
    """Process multi part"""
    if resume:
        start_part = get_last_successful_part(output_path) + 1
    else:
        start_part = 0

    wat_count = len(wat_index_files)
    wat_per_part = math.ceil(wat_count / multipart)
    part_paths = []
    for i in range(start_part, multipart):
        start = i * wat_per_part
        end = (i + 1) * wat_per_part
        part_path = f"{output_path}/part_{i}"
        part_paths.append(part_path)
        logger.info(f"Processing part {i} from {start} to {end} into {part_path}")
        process_one_part(part_path, wat_index_files[start:end], build_spark, False, document_type, source_cc_protocol)

    spark = build_spark()
    logger.info("Merging parts")
    df = None
    part_paths = [f"{output_path}/part_{i}" for i in range(0, multipart)]
    for part_path in part_paths:
        if df is None:
            df = spark.read.parquet(part_path)
        else:
            df = df.union(spark.read.parquet(part_path))

    deduplicate_repartition_count(df, output_path + "/merged", wat_count, spark, shuffle)


def get_date_str():
    return datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def cc2dataset(
    output_path,
    wat_index_count=1,
    wat_count=100,
    master="local",
    num_cores=128,
    mem_gb=256,
    multipart=None,
    shuffle=True,
    resume=None,
    spark_builder=None,
    document_type="image",
    source_cc_protocol="s3",
):
    """Convert common crawl to image caption set"""

    if resume is not None and multipart is None:
        raise ValueError("Cannot resume without multipart")

    if resume is None:
        job_id = get_date_str()
        logger.info(f"JOB ID: {job_id}")
        output_path = f"{output_path}/{job_id}"
    else:
        output_path = resume

    logger.info(f"Writing in: {output_path}")

    if spark_builder is None:
        spark_builder = lambda: build_spark_session(master, num_cores, mem_gb)

    def build_spark():
        spark = SparkSession.getActiveSession()
        if spark is not None:
            spark.stop()
        return spark_builder()

    if resume is None:
        wat_index_files = read_wat_index_files(wat_index_count, wat_count, source_cc_protocol)
        # write wat index files to disk in output_path with fsspec
        with fsspec.open(f"{output_path}/wat_index_files.txt", "w", encoding="utf8") as f:
            f.write("\n".join(wat_index_files))
    else:
        with fsspec.open(f"{output_path}/wat_index_files.txt", "r", encoding="utf8") as f:
            wat_index_files = f.read().splitlines()

    if multipart is None:
        process_one_part(output_path, wat_index_files, build_spark, shuffle, document_type, source_cc_protocol)
    else:
        process_multi_part(
            output_path, wat_index_files, build_spark, multipart, shuffle, resume, document_type, source_cc_protocol
        )


def main():
    fire.Fire(cc2dataset)


if __name__ == "__main__":
    main()
