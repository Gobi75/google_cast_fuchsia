# System Fixes Documentation

## CATT Library Copy (Timeout Fix)
This folder contains a local copy of the `catt` library with an improved timeout (10s). 
If the changes disappear after an HA update or a system failure, use the following command to restore the patched version to the container:

```bash
docker exec -it homeassistant cp -r /config/custom_components/continuously_casting_dashboards/catt /usr/local/lib/python3.13/site-packages/
```

System Location:
/usr/local/lib/python3.13/site-packages/catt
