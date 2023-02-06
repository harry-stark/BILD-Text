from bildtext import process_wet
import os
import pandas as pd

if __name__ == "__main__":
    from_s3 = True
    wet = "s3://commoncrawl/crawl-data/CC-MAIN-2022-33/segments/1659882570651.49/wet/CC-MAIN-20220807150925-20220807180925-00000.warc.wet.gz"
    if from_s3:
        url =  wat
    else:
        url = "https://data.commoncrawl.org/" + wat

    results = process_wet(url)
    df = pd.DataFrame(results, columns=[ "text","url","uid"])
    df.to_parquet(os.getcwd() + "/output.parquet")
    print(df)
