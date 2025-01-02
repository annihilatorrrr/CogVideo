import torch

from pathlib import Path
from typing import Any, Dict, List, Tuple, Callable
from typing_extensions import override

from accelerate.logging import get_logger
from torch.utils.data import Dataset
from torchvision import transforms

from finetune.constants import LOG_NAME, LOG_LEVEL

from .utils import (
    load_prompts, load_videos,
    preprocess_video_with_resize,
    preprocess_video_with_buckets
)

# Must import after torch because this can sometimes lead to a nasty segmentation fault, or stack smashing error
# Very few bug reports but it happens. Look in decord Github issues for more relevant information.
import decord  # isort:skip

decord.bridge.set_bridge("torch")

logger = get_logger(LOG_NAME, LOG_LEVEL)


class BaseT2VDataset(Dataset):
    """
    Base dataset class for Text-to-Video (T2V) training.

    This dataset loads prompts and videos for T2V training.

    Args:
        data_root (str): Root directory containing the dataset files
        caption_column (str): Path to file containing text prompts/captions
        video_column (str): Path to file containing video paths
        device (torch.device): Device to load the data on
        encode_video_fn (Callable[[torch.Tensor], torch.Tensor], optional): Function to encode videos
    """

    def __init__(
        self,
        data_root: str,
        caption_column: str,
        video_column: str,
        device: torch.device = None,
        encode_video_fn: Callable[[torch.Tensor], torch.Tensor] = None,
        *args,
        **kwargs
    ) -> None:
        super().__init__()

        data_root = Path(data_root)
        self.prompts = load_prompts(data_root / caption_column)
        self.videos = load_videos(data_root / video_column)
        self.device = device
        self.encode_video_fn = encode_video_fn

        # Check if all video files exist
        if any(not path.is_file() for path in self.videos):
            raise ValueError(
                f"Some video files were not found. Please ensure that all video files exist in the dataset directory. Missing file: {next(path for path in self.videos if not path.is_file())}"
            )

        # Check if number of prompts matches number of videos
        if len(self.videos) != len(self.prompts):
            raise ValueError(
                f"Expected length of prompts and videos to be the same but found {len(self.prompts)=} and {len(self.videos)=}. Please ensure that the number of caption prompts and videos match in your dataset."
            )

    def __len__(self) -> int:
        return len(self.videos)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        if isinstance(index, list):
            # Here, index is actually a list of data objects that we need to return.
            # The BucketSampler should ideally return indices. But, in the sampler, we'd like
            # to have information about num_frames, height and width. Since this is not stored
            # as metadata, we need to read the video to get this information. You could read this
            # information without loading the full video in memory, but we do it anyway. In order
            # to not load the video twice (once to get the metadata, and once to return the loaded video
            # based on sampled indices), we cache it in the BucketSampler. When the sampler is
            # to yield, we yield the cache data instead of indices. So, this special check ensures
            # that data is not loaded a second time. PRs are welcome for improvements.
            return index

        prompt = self.prompts[index]
        video = self.videos[index]

        latent_dir = video.parent / "latent"
        latent_dir.mkdir(parents=True, exist_ok=True)
        encoded_video_path = latent_dir / (video.stem + ".pt")

        if encoded_video_path.exists():
            # shape of encoded_video: [C, F, H, W]
            encoded_video = torch.load(encoded_video_path, weights_only=True)
        else:
            frames = self.preprocess(video)
            frames = frames.to(self.device)
            # current shape of frames: [F, C, H, W]
            frames = self.video_transform(frames)
            # Convert to [B, C, F, H, W]
            frames = frames.unsqueeze(0)
            frames = frames.permute(0, 2, 1, 3, 4).contiguous()
            encoded_video = self.encode_video_fn(frames)
            # [B, C, F, H, W] -> [C, F, H, W]
            encoded_video = encoded_video[0].cpu()
            torch.save(encoded_video, encoded_video_path)
            logger.info(f"Saved encoded video to {encoded_video_path}", main_process_only=False)

        return {
            "prompt": prompt,
            "encoded_video": encoded_video,
            "video_metadata": {
                "num_frames": encoded_video.shape[1],
                "height": encoded_video.shape[2],
                "width": encoded_video.shape[3],
            },
        }

    def preprocess(self, video_path: Path) -> torch.Tensor:
        """
        Loads and preprocesses a video.

        Args:
            video_path: Path to the video file to load.

        Returns:
            torch.Tensor: Video tensor of shape [F, C, H, W] where:
                - F is number of frames
                - C is number of channels (3 for RGB)
                - H is height
                - W is width
        """
        raise NotImplementedError("Subclass must implement this method")
    
    def video_transform(self, frames: torch.Tensor) -> torch.Tensor:
        """
        Applies transformations to a video.

        Args:
            frames (torch.Tensor): A 4D tensor representing a video
                with shape [F, C, H, W] where:
                - F is number of frames
                - C is number of channels (3 for RGB)
                - H is height 
                - W is width

        Returns:
            torch.Tensor: The transformed video tensor with the same shape as the input
        """
        raise NotImplementedError("Subclass must implement this method")


class T2VDatasetWithResize(BaseT2VDataset):
    """
    A dataset class for text-to-video generation that resizes inputs to fixed dimensions.

    This class preprocesses videos by resizing them to specified dimensions:
    - Videos are resized to max_num_frames x height x width

    Args:
        max_num_frames (int): Maximum number of frames to extract from videos
        height (int): Target height for resizing videos
        width (int): Target width for resizing videos
    """

    def __init__(self, max_num_frames: int, height: int, width: int, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.max_num_frames = max_num_frames
        self.height = height
        self.width = width

        self.__frame_transform = transforms.Compose(
            [
                transforms.Lambda(lambda x: x / 255.0 * 2.0 - 1.0)
            ]
        )
    
    @override
    def preprocess(self, video_path: Path) -> torch.Tensor:
        return preprocess_video_with_resize(
            video_path, self.max_num_frames, self.height, self.width,
        )
    
    @override
    def video_transform(self, frames: torch.Tensor) -> torch.Tensor:
        return torch.stack([self.__frame_transform(f) for f in frames], dim=0)


class T2VDatasetWithBuckets(BaseT2VDataset):

    def __init__(
        self,
        video_resolution_buckets: List[Tuple[int, int, int]],
        vae_temporal_compression_ratio: int,
        vae_height_compression_ratio: int,
        vae_width_compression_ratio: int,
        *args, **kwargs
    ) -> None:
        """
        
        """
        super().__init__(*args, **kwargs)

        self.video_resolution_buckets = [
            (
                int(b[0] / vae_temporal_compression_ratio),
                int(b[1] / vae_height_compression_ratio),
                int(b[2] / vae_width_compression_ratio),
            )
            for b in video_resolution_buckets
        ]

        self.__frame_transform = transforms.Compose(
            [
                transforms.Lambda(lambda x: x / 255.0 * 2.0 - 1.0)
            ]
        )
    
    @override
    def preprocess(self, video_path: Path) -> torch.Tensor:
        return preprocess_video_with_buckets(
            video_path, self.video_resolution_buckets
        )
    
    @override
    def video_transform(self, frames: torch.Tensor) -> torch.Tensor:
        return torch.stack([self.__frame_transform(f) for f in frames], dim=0)