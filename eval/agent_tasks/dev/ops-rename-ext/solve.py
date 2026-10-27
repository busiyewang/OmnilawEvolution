import os, sys
for root, _, files in os.walk(os.path.join(sys.argv[1], "photos")):
    for name in files:
        base, ext = os.path.splitext(name)
        if ext.lower() == ".jpeg":
            os.rename(os.path.join(root, name), os.path.join(root, base + ".jpg"))
