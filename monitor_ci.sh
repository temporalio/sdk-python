#!/bin/bash
run_id=17270021185
echo "Monitoring CI run $run_id..."
echo "URL: https://github.com/temporalio/sdk-python/actions/runs/$run_id"

check_count=0
while true; do
    check_count=$((check_count + 1))
    echo -e "\n--- Check #$check_count at $(date) ---"
    
    # Get run status
    status_info=$(gh run view $run_id --json status,conclusion --jq '"\(.status):\(.conclusion)"')
    echo "Status: $status_info"
    
    # If completed, check for failures and logs
    if [[ $status_info == "completed:"* ]]; then
        echo "Run completed!"
        
        # Get all job statuses
        echo -e "\nJob statuses:"
        gh run view $run_id --json jobs --jq '.jobs[] | "\(.name): \(.conclusion)"'
        
        # Check for any failures
        if [[ $status_info == *"failure"* ]]; then
            echo -e "\n!!! FAILURE DETECTED - Checking for interleaved history output..."
            
            # Download logs and search for interleaved history output
            echo "Downloading logs..."
            gh run view $run_id --log > ci_logs_$run_id.txt
            
            # Search for interleaved history patterns
            if grep -A 50 "caller-wf-.*handler-wf-" ci_logs_$run_id.txt > interleaved_output.txt; then
                echo -e "\n!!! FOUND INTERLEAVED HISTORY OUTPUT !!!"
                terminal-notifier -title "CI Monitor" -message "Found interleaved history output in failed CI run!" -sound default
                echo "Saved to interleaved_output.txt"
                echo "First 100 lines:"
                head -100 interleaved_output.txt
            else
                echo "No interleaved history output found in logs"
            fi
        fi
        
        terminal-notifier -title "CI Monitor" -message "CI run $run_id completed with status: $status_info" -sound default
        break
    fi
    
    # Sleep before next check
    echo "Sleeping for 30 seconds..."
    sleep 30
done
