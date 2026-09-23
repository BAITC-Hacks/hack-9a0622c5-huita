import uvicorn

if __name__ == "__main__":
    uvicorn.run("beesmart.web:app", host="127.0.0.1", port=8000, workers=1, access_log=False)
