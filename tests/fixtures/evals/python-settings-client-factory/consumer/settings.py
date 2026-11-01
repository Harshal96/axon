import directory_sdk
from helpers import get_V1Api

def GET_DIRECTORY_API():
    return get_V1Api(directory_sdk, "REMOTE_URL")

def get_directory_api():
    return get_V1Api(client=directory_sdk)

def dynamic_api(package):
    return get_V1Api(package)
