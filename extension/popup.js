// Get DOM elements
const downloadBtn = document.getElementById('DownloadBtn');
const resultDiv = document.getElementById('result');
const loader = document.getElementById('loader');
const currentUrlContainer = document.getElementById('currentUrlContainer');
const urlInputContainer = document.getElementById('urlInputContainer');
const currentUrlText = document.getElementById('currentUrl');
const urlInput = document.getElementById('urlInput');
const folderInput = document.getElementById('folderInput');

let currentTabUrl = '';
let isYouTubePage = false;

// Check if URL is a YouTube URL
function isYouTubeUrl(url) {
  return url && (
    url.includes('youtube.com/watch') ||
    url.includes('youtu.be/') ||
    url.includes('youtube.com/shorts/')
  );
}

// Initialize popup - get current tab URL
browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
  if (tabs[0]) {
    currentTabUrl = tabs[0].url;
    isYouTubePage = isYouTubeUrl(currentTabUrl);

    if (isYouTubePage) {
      // Show current URL container
      currentUrlText.textContent = currentTabUrl;
      currentUrlContainer.style.display = 'block';
      urlInputContainer.classList.remove('show');
    } else {
      // Show URL input container
      currentUrlContainer.style.display = 'none';
      urlInputContainer.classList.add('show');
    }
  }
}).catch((error) => {
  console.error('Error getting tab info:', error);
  currentUrlText.textContent = 'Error loading URL';
});

// Handle download button click
downloadBtn.addEventListener('click', () => {
  let urlToDownload = '';

  // Determine which URL to use
  if (isYouTubePage) {
    urlToDownload = currentTabUrl;
  } else {
    urlToDownload = urlInput.value.trim();
  }

  // Validate URL
  if (!urlToDownload) {
    showResult('Please enter a YouTube URL', 'error');
    return;
  }

  if (!isYouTubeUrl(urlToDownload)) {
    showResult('Please enter a valid YouTube URL', 'error');
    return;
  }

  // Start download
  startDownload(urlToDownload);
});

// Start download process
function startDownload(url) {
  console.log('Starting download for:', url);

  // Get folder path (optional)
  const folderPath = folderInput.value.trim();

  // Show loading state
  downloadBtn.disabled = true;
  loader.style.display = 'block';
  resultDiv.classList.remove('show', 'success', 'error', 'warning');

  try {
    const port = browser.runtime.connectNative('app_manifest');

    port.onMessage.addListener((response) => {
      console.log('Received response:', response);

      // Hide loader
      loader.style.display = 'none';
      downloadBtn.disabled = false;

      if (response.status === 'success') {
        showResult(`Downloaded: ${response.filename || 'YouTube MP3'}`, 'success');
      } else if (response.status === 'error') {
        showResult(`Error: ${response.message || 'Download failed'}`, 'error');
      } else if (response.status === 'warning') {
        showResult(`Warning: ${response.message || 'Download completed with warnings'}`, 'warning');
      } else {
        showResult('Download completed', 'success');
      }
    });

    port.onDisconnect.addListener(() => {
      const error = browser.runtime.lastError;
      console.error('Disconnect error:', error);

      loader.style.display = 'none';
      downloadBtn.disabled = false;

      if (error) {
        showResult(`Connection error: ${error.message}`, 'error');
      }
    });

    // Send download request with optional folder
    const message = { url: url };
    if (folderPath) {
      message.folder = folderPath;
    }
    port.postMessage(message);

  } catch (error) {
    console.error('Error starting download:', error);
    loader.style.display = 'none';
    downloadBtn.disabled = false;
    showResult(`Error: ${error.message}`, 'error');
  }
}

// Show result message
function showResult(message, type) {
  resultDiv.textContent = message;
  resultDiv.classList.add('show', type);

  // Auto-hide after 5 seconds for success messages
  if (type === 'success') {
    setTimeout(() => {
      resultDiv.classList.remove('show');
    }, 5000);
  }
}
