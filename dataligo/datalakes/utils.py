from io import BytesIO
from pathlib import Path
import pandas as pd
from ..exceptions import ExtensionNotSupportException
from boto3.s3.transfer import TransferConfig
import os
import threading
import sys

multipart_config = TransferConfig(multipart_threshold=1024 * 50, 
                        max_concurrency=8,
                        multipart_chunksize=1024 * 10,
                        use_threads=True)


def _multi_file_load(s3,bucket,key,reader,extension,pandas_args):
    key = key.strip('/*').strip('*').strip('/')
    bucket = s3.Bucket(bucket)
    pfx_objs = bucket.objects.filter(Prefix=key)
    pfx_dfs = []
    for obj in pfx_objs:
        if obj.key.endswith('/'):
            continue
        body = BytesIO(obj.get()['Body'].read())
        df = reader(body, **pandas_args)
        pfx_dfs.append(df)
    return pfx_dfs

def _s3_upload_folder(s3, local_folder_path, bucket, key):
    key = key.rstrip('/')+'/'+Path(local_folder_path).stem
    for root, _ , files in os.walk(local_folder_path):
        for filename in files:
            local_path = os.path.join(root, filename)
            relative_path = os.path.relpath(local_path, local_folder_path)
            relative_path = relative_path.replace("\\", "/")
            s3_path = os.path.join(key, relative_path)
            s3.Object(bucket, s3_path).upload_file(local_path,
                            Config=multipart_config
                            )

def _s3_download_folder(s3,s3_path, bucket, key, local_path_to_download='.'):
    if s3_path:
        bucket, key =  s3_path.split('/',3)[2:]
    my_bucket = s3.Bucket(bucket)
    folder_to_download = Path(key).stem
    local_path = os.path.join(local_path_to_download, folder_to_download)
    os.makedirs(local_path)
    for obj in my_bucket.objects.filter(Prefix=key):
        key = obj.key
        if key.endswith('/'):
            os.makedirs(os.path.join(local_path,key))
        else:
            s3.Object(bucket, key).download_file(os.path.join(local_path,key),Config=multipart_config)
    print("Folder downloaded to the path:", f"{local_path}")

# source: https://medium.com/analytics-vidhya/aws-s3-multipart-upload-download-using-boto3-python-sdk-2dedb0945f11
# source: https://boto3.amazonaws.com/v1/documentation/api/latest/_modules/boto3/s3/transfer.html
def _s3_upload_file(s3, file_path, bucket, key):
    suffix = Path(file_path).suffix
    if suffix:
        extension=suffix[1:]
    s3.Object(bucket, key).upload_file(file_path,
                            ExtraArgs={'ContentType': f'text/{extension}'},
                            Config=multipart_config,
                            Callback=ProgressPercentage(file_path)
                            )
    print('\n')

 # source: https://medium.com/analytics-vidhya/aws-s3-multipart-upload-download-using-boto3-python-sdk-2dedb0945f11
 # source: https://boto3.amazonaws.com/v1/documentation/api/latest/_modules/boto3/s3/transfer.html
def _s3_download_file(s3, s3_path=None, bucket=None, key=None, path_to_download='.'):
    if s3_path:
        bucket, key =  s3_path.split('/',3)[2:]
        filename = key.split('/')[-1]
    else:
        filename = key.split('/')[-1]
    file_path = os.path.join(path_to_download,filename)
    s3.Object(bucket, key).download_file(file_path,
                            Config=multipart_config,
                            Callback=ProgressPercentage(file_path)
                            )
    print('\n')
    print("File downloaded to the path:", f"{file_path}")

def _s3_writer(s3, df, bucket, filename, extension, index=False, sep=','):
    buf = BytesIO()
    suffix = Path(filename).suffix
    if suffix:
        extension = suffix[1:]
    extension = extension.lower()
    if extension=='csv':
        df.to_csv(buf, index=index, sep=sep)
    elif extension=='parquet':
        df.to_parquet(buf, index=index)
    elif extension=='json':
        df.to_json(buf)
    elif extension=='feather':
        df.to_feather(buf)
    elif extension in ['xlsx','xls']:
        df.to_excel(buf, index=index)
    else:
        raise ExtensionNotSupportException(f'Unsupported Extension: {extension}')
    buf.seek(0)
    s3.Bucket(bucket).put_object(Key=filename, Body=buf.getvalue())
    
def _gcs_writer(gcs, df, bucket, filename, extension, index=False, sep=','):
    suffix = Path(filename).suffix
    if suffix:
        extension = suffix[1:]
    bucket = gcs.get_bucket(bucket)
    extension = extension.lower()
    if extension=='csv':
        bucket.blob(filename).upload_from_string(df.to_csv(index=index, sep=sep), 'text/csv')
    elif extension=='parquet':
        bucket.blob(filename).upload_from_string(df.to_parquet(index=index), 'text/parquet')
    elif extension=='json':
        bucket.blob(filename).upload_from_string(df.to_json(), 'text/json')
    # elif extension=='feather':
    #     bucket.blob(filename).upload_from_string(df.to_feather(), 'text/feather')
    # elif extension in ['xlsx','xls']:
    #     bucket.blob(filename).upload_from_string(df.to_excel(), 'text/xlsx')
    else:
        raise ExtensionNotSupportException(f'Unsupported Extension: {extension}')

def _azure_blob_writer(abs, df, container_name,blob_name, extension, overwrite=True, index=False, sep=','):
    suffix = Path(blob_name).suffix
    if suffix:
        extension = suffix[1:]
    container_client = abs.get_container_client(container_name)
    blob_client = container_client.get_blob_client(blob_name)
    buf = BytesIO()
    if extension=='csv':
        df.to_csv(buf,encoding='utf-8',index=index,sep=sep)
    elif extension=='json':
        df.to_json(buf)
    elif extension=='parquet':
        df.to_parquet(buf)
    elif extension=='feather':
        df.to_feather(buf)
    elif extension in ['xlsx','xls']:
        df.to_excel(buf)
    else:
        raise ExtensionNotSupportException(f'Unsupported Extension: {extension}')
    buf.seek(0)
    blob_client.upload_blob(buf.getvalue(), overwrite=overwrite)

# source: https://medium.com/analytics-vidhya/aws-s3-multipart-upload-download-using-boto3-python-sdk-2dedb0945f11
# source: https://boto3.amazonaws.com/v1/documentation/api/latest/_modules/boto3/s3/transfer.html
class ProgressPercentage(object):
    def __init__(self, filename):
        self._filename = filename
        self._size = float(os.path.getsize(filename))
        self._seen_so_far = 0
        self._lock = threading.Lock()

    def __call__(self, bytes_amount):
        # To simplify we'll assume this is hooked up
        # to a single filename.
        with self._lock:
            self._seen_so_far += bytes_amount
            percentage = (self._seen_so_far / self._size) * 100
            sys.stdout.write(
                "\r%s  %s / %s  (%.2f%%)" % (
                    self._filename, self._seen_so_far, self._size,
                    percentage))
            sys.stdout.flush()