#!/bin/bash

echo $BASHPID > pid
touch zombies.log

while true ; do
    echo "// $(date)" >> zombies.log
    ps aux | awk '{if($8=="Z") print}' >> zombies.log 2>&1
    sleep 5
done
