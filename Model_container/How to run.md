1. View all API activity:
cd "/Users/sepehrmortazavi/Desktop/Master thesis /POC_version1/Project_1"
docker compose logs -f cellpose-api

2. Wait for it to be ready (first time takes 5-10 min for model download):
   # Check if ready
curl http://localhost:8002/health

3. Process your sample.png:
   
   cd "/Users/sepehrmortazavi/Desktop/Master thesis /POC_version1"
curl -X POST "http://localhost:8002/segment" \
  -F "image=@sample.png" \ #you can change the file name 
  -F "flow_threshold=0.4" \
  -F "cellprob_threshold=0.0" \
  --output masks.npy # please change name of mask file each time .

  4.  run /Users/sepehrmortazavi/Desktop/Master thesis /POC_version1/view_masks.py 
   view_maskes.py to see the file with napari a tool coverterfrom .npy to .jpg or .png 