from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.conf import settings
import requests
import numpy as np
import io
from .models import SegmentationResult
from .forms import ImageUploadForm

@require_http_methods(["GET", "POST"])
def index(request):
    if request.method == 'POST':
        form = ImageUploadForm(request.POST, request.FILES)
        if form.is_valid():
            result = form.save(commit=False)
            result.status = 'processing'
            result.save()
            
            try:
                process_segmentation(result)
                result.status = 'completed'
                messages.success(request, 'Image segmented successfully!')
            except Exception as e:
                result.status = 'failed'
                result.error_message = str(e)
                messages.error(request, f'Error: {str(e)}')
            
            result.save()
            return redirect('result', pk=result.pk)
    else:
        form = ImageUploadForm()
    
    recent_results = SegmentationResult.objects.all()[:5]
    return render(request, 'index.html', {
        'form': form,
        'recent_results': recent_results
    })

@require_http_methods(["GET"])
def result(request, pk):
    result = get_object_or_404(SegmentationResult, pk=pk)
    return render(request, 'result.html', {'result': result})

@require_http_methods(["GET"])
def history(request):
    results = SegmentationResult.objects.all()
    return render(request, 'history.html', {'results': results})

def process_segmentation(result_obj):
    if not result_obj.image:
        raise ValueError("No image provided")
    
    result_obj.image.seek(0)
    files = {'image': result_obj.image.read()}
    data = {
        'flow_threshold': result_obj.flow_threshold,
        'cellprob_threshold': result_obj.cellprob_threshold,
    }
    
    api_url = f"{settings.MODEL_API_URL}{settings.MODEL_SEGMENT_ENDPOINT}"
    
    response = requests.post(api_url, files=files, data=data)
    response.raise_for_status()
    
    mask_data = response.content
    
    filename = f'mask_result_{result_obj.id}.npy'
    result_obj.result_mask.save(filename, io.BytesIO(mask_data), save=False)
