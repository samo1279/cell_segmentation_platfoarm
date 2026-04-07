from django.db import models

class SegmentationResult(models.Model):
    image = models.ImageField(upload_to='uploads/')
    result_mask = models.FileField(upload_to='results/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    flow_threshold = models.FloatField(default=0.4)
    cellprob_threshold = models.FloatField(default=0.0)
    status = models.CharField(
        max_length=20,
        choices=[('processing', 'Processing'), ('completed', 'Completed'), ('failed', 'Failed')],
        default='processing'
    )
    error_message = models.TextField(blank=True, null=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Segmentation {self.id} - {self.status}"
